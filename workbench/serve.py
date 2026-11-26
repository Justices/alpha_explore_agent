"""Serve the workbench and launch bounded Regular research on loopback."""

import argparse
from http.cookies import CookieError, SimpleCookie
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
from secrets import token_urlsafe
from threading import Lock, Thread
from time import monotonic
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

WORKBENCH = Path(__file__).resolve().parent
PROJECT = WORKBENCH.parent
if not __package__:
    sys.path.insert(0, str(PROJECT))

from alpha_research.progress import PROGRESS_ENV, write_progress
from alpha_research.state.paths import default_state_root, expand_path
from alpha_research.state.operation_lock import SubmissionGateError
from alpha_research.state.store import atomic_write_json, update_stop_flag
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError
from alpha_research.workflows.regular.campaign import (
    campaign_can_resume, delete_saved_campaign, load_saved_campaign,
)
from workbench.runtime_models import checked_selection, listed_models


COOKIE_NAME = "alpha_workbench_session"
SESSION_SECONDS = 86400


def agent_command(agent_args, command):
    return [sys.executable, "-B", "-m", "alpha_research", *agent_args, *command]


def agent_env():
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


def read_status(agent_args):
    result = subprocess.run(
        agent_command(agent_args, ["status"]),
        cwd=PROJECT,
        env=agent_env(),
        capture_output=True,
        check=True,
        encoding="utf-8",
        timeout=20,
    )
    return json.loads(result.stdout)


def campaign_arguments(data):
    required = ("region", "objective")
    optional_text = ("category", "universe")
    if (not isinstance(data, dict)
            or not all(key in data for key in required)
            or set(data) - {*required, *optional_text, "dataset_ids", "delay", "ppa", "max_batches"}):
        raise ValueError("需要 Region 和研究目标")
    for key in ("region", "objective", *optional_text):
        if key in data and (not isinstance(data[key], str) or not data[key].strip()
                            or "\x00" in data[key]):
            raise ValueError(f"{key} 必须是非空文本")
    if "max_batches" in data and (type(data["max_batches"]) is not int or data["max_batches"] < 1):
        raise ValueError("批次预算必须是正整数")
    if "delay" in data and (type(data["delay"]) is not int or data["delay"] != 1):
        raise ValueError("Regular 研究仅支持 Delay 1")
    datasets = data.get("dataset_ids", [])
    if (not isinstance(datasets, list)
            or any(not isinstance(value, str) or not value.strip() or "\x00" in value
                   for value in datasets)
            or len(set(datasets)) != len(datasets)):
        raise ValueError("Dataset ID 必须是互不重复的非空文本")
    ppa = data.get("ppa", False)
    if type(ppa) is not bool:
        raise ValueError("PPA Profile 必须显式启用")
    args = ["run-campaign"]
    for key in (*required, "max_batches", "category", "delay", "universe"):
        if key in data:
            args.append(f"--{key.replace('_', '-')}={data[key]}")
    args.extend(f"--dataset={dataset}" for dataset in datasets)
    if ppa:
        args.append("--ppa")
    return args


def research_arguments(data):
    """Build a validated CLI command for one of the three research workflows."""
    if not isinstance(data, dict):
        raise ValueError("研究启动请求必须是 JSON 对象")
    workflow = data.get("workflow", "regular")
    if workflow == "regular":
        return workflow, campaign_arguments({
            key: value for key, value in data.items() if key != "workflow"
        }), None

    if workflow == "super":
        allowed = {"workflow", "region", "objective", "delay", "max_iterations"}
        if set(data) - allowed:
            raise ValueError("Super 启动参数包含不支持的字段")
        for key in ("region", "objective"):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"{key} 必须是非空文本")
        delay = data.get("delay")
        if delay is not None and (type(delay) is not int or delay < 0):
            raise ValueError("Super Delay 必须是非负整数")
        iterations = data.get("max_iterations", 1)
        if type(iterations) is not int or not 1 <= iterations <= 20:
            raise ValueError("Super 迭代预算必须是 1 到 20 的整数")
        args = [
            "run-super", f"--region={data['region'].strip()}",
            f"--objective={data['objective'].strip()}",
            f"--max-iterations={iterations}",
        ]
        if delay is not None:
            args.append(f"--delay={delay}")
        return workflow, args, None

    if workflow == "optimization":
        allowed = {"workflow", "alpha_id", "objective", "region", "max_iterations"}
        if set(data) - allowed:
            raise ValueError("Alpha 优化启动参数包含不支持的字段")
        for key in ("alpha_id", "objective"):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"{key} 必须是非空文本")
        region = data.get("region")
        if region is not None and (
                not isinstance(region, str) or not region.strip() or "\x00" in region):
            raise ValueError("Region 必须是非空文本")
        iterations = data.get("max_iterations", 1)
        if type(iterations) is not int or not 1 <= iterations <= 20:
            raise ValueError("Alpha 优化迭代预算必须是 1 到 20 的整数")
        args = [
            "run-optimization", f"--alpha-id={data['alpha_id'].strip()}",
            f"--objective={data['objective'].strip()}",
            f"--max-iterations={iterations}",
        ]
        if region:
            args.append(f"--region={region.strip()}")
        return workflow, args, data["alpha_id"].strip()

    raise ValueError("不支持的研究工作流")


def _workbench_records_path(root):
    return Path(root) / "state" / "workbench" / "records.json"


def _validate_workbench_records(payload):
    if (not isinstance(payload, dict) or set(payload) != {"version", "cases", "alphas"}
            or payload.get("version") != 1
            or not isinstance(payload.get("cases"), list)
            or not isinstance(payload.get("alphas"), list)):
        raise ValueError("工作台记录格式无效")
    for name in ("cases", "alphas"):
        items = payload[name]
        if len(items) > 10000:
            raise ValueError("工作台记录数量超出限制")
        ids = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("工作台记录必须是对象")
            record_id = item.get("id")
            if not isinstance(record_id, str) or not record_id.strip() or record_id in ids:
                raise ValueError("工作台记录 ID 缺失或重复")
            ids.add(record_id)
    try:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("工作台记录包含无法保存的数据") from exc
    if len(encoded) > 5 * 1024 * 1024:
        raise ValueError("工作台记录超过 5 MB")
    return payload


def load_workbench_records(root):
    path = _workbench_records_path(root)
    state_dir = Path(root) / "state"
    if state_dir.is_symlink() or path.parent.is_symlink() or path.is_symlink():
        raise ValueError("工作台记录路径不能是符号链接")
    if not path.exists():
        return {"version": 1, "cases": [], "alphas": []}
    if path.stat().st_size > 5 * 1024 * 1024:
        raise ValueError("工作台记录超过 5 MB")
    return _validate_workbench_records(json.loads(path.read_text(encoding="utf-8")))


def save_workbench_records(root, payload):
    payload = _validate_workbench_records(payload)
    path = _workbench_records_path(root)
    state_dir = Path(root) / "state"
    if state_dir.is_symlink():
        raise ValueError("工作台状态目录不能是符号链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise ValueError("工作台记录路径不能是符号链接")
    with SingleWriterLock(state_dir / "workbench-records.lock"):
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError("工作台记录路径不能是符号链接")
        atomic_write_json(path, payload)
    return {"saved": True, "case_count": len(payload["cases"]),
            "alpha_count": len(payload["alphas"])}


def recent_progress(path):
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            stream.seek(max(0, size - 65536))
            lines = stream.read(65536).splitlines()
    except OSError:
        return []
    events = []
    for line in lines[-60:]:
        try:
            event = json.loads(line)
        except (UnicodeError, ValueError):
            continue
        if (isinstance(event, dict) and isinstance(event.get("time"), str)
                and isinstance(event.get("message"), str)):
            events.append(event)
    return events


def handler_for(agent_args, port):
    origin = f"http://127.0.0.1:{port}"
    launch_lock = Lock()
    session_lock = Lock()
    sessions = {}
    launch = {"status": "idle"}
    root = (expand_path(agent_args[agent_args.index("--state-root") + 1]).resolve()
            if "--state-root" in agent_args else default_state_root())
    log_dir = root / "state" / "logs" / "workbench"

    def launch_stop_flag(launch_id):
        return root / "state" / f"workbench-{launch_id}.stop"

    def run_campaign(args, credentials, log_path, selection, workflow="regular"):
        outcome = {"status": "failed", "workflow": workflow}
        stop_flag = launch_stop_flag(log_path.stem)
        try:
            write_progress("正在启动研究进程", log_path)
            command = []
            if selection:
                command.extend([
                    "--lead", selection["lead"], "--executor", selection["executor"],
                ])
            command.extend([*args, "--brain-credentials-stdin"])
            result = subprocess.run(
                agent_command(agent_args, command),
                cwd=PROJECT, env={**agent_env(), PROGRESS_ENV: str(log_path),
                                  "ALPHA_RESEARCH_LAUNCH_STOP": str(stop_flag)},
                input=json.dumps(credentials, ensure_ascii=False),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8")
            outcome["exit_code"] = result.returncode
            if result.returncode == 0:
                report = json.loads(result.stdout)
                if isinstance(report, dict) and isinstance(report.get("status"), str):
                    outcome["status"] = "exited"
                    outcome["result_status"] = report["status"]
                    if workflow == "regular":
                        outcome["campaign_status"] = report["status"]
                    for key in (
                        "campaign_id", "run_id", "branch_id", "source_alpha_id",
                        "candidate_alpha_id", "alpha_id", "iteration_session_id",
                        "iterations_completed", "iteration_budget", "iteration_stop_reason",
                        "iteration_ref", "record_ref", "report_ref", "simulation_intent_ref",
                        "receipt_ref", "readback_ref", "audit_ref", "candidate_status",
                        "preferred_status",
                    ):
                        value = report.get(key)
                        if isinstance(value, (str, int, float, bool, dict)):
                            outcome[key] = value
        except (OSError, ValueError, subprocess.SubprocessError):
            write_progress("无法启动研究进程或读取执行结果", log_path)
        finally:
            if outcome["status"] == "failed" and stop_flag.exists() and outcome.get("exit_code") == 2:
                outcome.update(status="exited", result_status="user_stopped")
                if workflow == "regular":
                    outcome["campaign_status"] = "user_stopped"
            if outcome["status"] == "failed":
                code = outcome.get("exit_code")
                write_progress(f"研究进程退出，退出码 {code}" if code is not None
                               else "研究进程启动失败", log_path)
            else:
                write_progress("研究进程已结束，结果见 Agent 状态与证据记录", log_path)
            with launch_lock:
                launch.update(outcome)
                try:
                    campaign_id = launch.get("campaign_id")
                    if campaign_id:
                        atomic_write_json(log_path.with_suffix(".campaign.json"),
                                          {"campaign_id": campaign_id})
                        if outcome["status"] == "exited" and selection:
                            atomic_write_json(
                                log_dir / (campaign_id + ".models.json"), {
                                    "campaign_id": campaign_id, **selection})
                    stop_flag.unlink(missing_ok=True)
                except OSError:
                    write_progress("无法保存本次启动的索引或清理终止标记", log_path)

    def start_campaign(args, credentials, selection, campaign_id=None,
                       workflow="regular", primary_id=None):
        launch.clear()
        launch.update(status="running", launch_id=uuid4().hex, workflow=workflow)
        if selection:
            launch.update(lead=selection["lead"], executor=selection["executor"])
        if campaign_id:
            launch["campaign_id"] = campaign_id
        if primary_id:
            launch["primary_id"] = primary_id
        log_path = log_dir / f"{launch['launch_id']}.jsonl"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path.touch(exist_ok=False)
            atomic_write_json(log_path.with_suffix(".launch.json"), {
                "args": args, "models": selection, "workflow": workflow,
                "primary_id": primary_id, "status": "starting"})
            Thread(target=run_campaign,
                   args=(args, credentials, log_path, selection, workflow),
                   daemon=True).start()
        except (OSError, RuntimeError):
            launch["status"] = "failed"
            return 503, {"error": "无法启动研究进程"}
        return 202, dict(launch)

    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, content_type, body, cookie=None):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def respond_json(self, code, payload, cookie=None):
            self.respond(code, "application/json; charset=utf-8",
                         json.dumps(payload, ensure_ascii=False).encode("utf-8"), cookie)

        def session_id(self):
            try:
                value = SimpleCookie(self.headers.get("Cookie", "")).get(COOKIE_NAME)
            except CookieError:
                return None
            return value.value if value else None

        def browser_credentials(self):
            with session_lock:
                now = monotonic()
                for token in [key for key, (expires, _) in sessions.items() if expires <= now]:
                    del sessions[token]
                entry = sessions.get(self.session_id())
                return entry[1] if entry else None

        def save_credentials(self, request):
            if (not isinstance(request, dict) or set(request) != {"username", "password"}
                    or not all(isinstance(value, str) and value.strip()
                               for value in request.values())):
                raise ValueError("请输入 BRAIN 用户名和密码")
            token = token_urlsafe(32)
            with session_lock:
                sessions.pop(self.session_id(), None)
                sessions[token] = (monotonic() + SESSION_SECONDS, request)
            cookie = (f"{COOKIE_NAME}={token}; Max-Age={SESSION_SECONDS}; "
                      "Path=/api; HttpOnly; SameSite=Strict")
            self.respond_json(200, {"configured": True}, cookie)

        def do_POST(self):
            if (self.headers.get("Host") != f"127.0.0.1:{port}"
                    or self.headers.get("Origin") != origin):
                self.respond_json(403, {"error": "请通过本机工作台启动研究"})
                return
            if self.path not in {
                    "/api/launch", "/api/credentials", "/api/research",
                    "/api/workbench-records"}:
                self.send_error(404)
                return
            if self.headers.get_content_type() != "application/json":
                self.respond_json(415, {"error": "请求必须使用 JSON"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                maximum = 5 * 1024 * 1024 if self.path == "/api/workbench-records" else 16384
                if not 0 < length <= maximum:
                    raise ValueError("请求大小无效")
                self.connection.settimeout(5)
                request = json.loads(self.rfile.read(length))
                if self.path == "/api/credentials":
                    self.browser_credentials()  # Remove expired entries before storing credentials.
                    self.save_credentials(request)
                    return
                if self.path == "/api/workbench-records":
                    saved = save_workbench_records(root, request)
                    self.respond_json(200, saved)
                    return
                credentials = self.browser_credentials()
                if credentials is None:
                    self.respond_json(401, {"error": "请先设置 BRAIN 凭证，凭证有效期为 24 小时"})
                    return
                if self.path == "/api/research":
                    self.research_action(request, credentials)
                    return
                if not isinstance(request, dict):
                    raise ValueError("研究启动请求必须是 JSON 对象")
                selection = checked_selection(
                    {key: request.get(key) for key in ("lead", "executor")},
                    agent_args, root)
                workflow, args, primary_id = research_arguments({
                    key: value for key, value in request.items()
                    if key not in ("lead", "executor")})
            except (OSError, UnicodeError, json.JSONDecodeError):
                self.respond_json(400, {"error": "无法读取请求"})
                return
            except ValueError as exc:
                self.respond_json(400, {"error": str(exc)})
                return
            with launch_lock:
                if launch["status"] == "running":
                    code, payload = 409, {"error": "已有研究任务正在运行"}
                else:
                    code, payload = start_campaign(
                        args, credentials, selection, workflow=workflow, primary_id=primary_id)
            self.respond_json(code, payload)

        def research_action(self, request, credentials):
            if not isinstance(request, dict) or not {"action", "id"} <= set(request):
                raise ValueError("需要研究操作和 ID")
            action, target = request["action"], request["id"]
            if action == "recover":
                if set(request) != {"action", "id", "workflow"}:
                    raise ValueError("回执核对需要工作流和 Simulation Intent 路径")
                workflow = request.get("workflow")
                if workflow not in {"super", "optimization"}:
                    raise ValueError("仅支持 Super 或 Alpha 优化回执核对")
                if not isinstance(target, str) or not target.strip():
                    raise ValueError("Simulation Intent 路径无效")
                target_path = Path(target).expanduser()
                if (not target_path.is_absolute() or target_path.is_symlink()
                        or target_path.parent.is_symlink()):
                    raise ValueError("Simulation Intent 必须是状态目录内的绝对路径")
                target_path = target_path.resolve()
                if not target_path.is_relative_to(root):
                    raise ValueError("Simulation Intent 位于当前 state-root 之外")
                with launch_lock:
                    if launch["status"] == "running":
                        code, payload = 409, {"error": "请等待当前任务结束"}
                    else:
                        code, payload = start_campaign(
                            ["recover-workflow", f"--workflow={workflow}", str(target_path)],
                            credentials, None, workflow=f"{workflow}_recovery",
                            primary_id=str(target_path))
                self.respond_json(code, payload)
                return

            if action == "resume":
                if set(request) != {"action", "id", "lead", "executor"}:
                    raise ValueError("恢复 Campaign 必须明确选择 Lead 与 Executor")
            elif action in {"stop", "delete"}:
                if set(request) != {"action", "id"}:
                    raise ValueError("研究操作参数无效")
            else:
                raise ValueError("研究操作无效")

            with launch_lock:
                running = launch["status"] == "running"
                if action == "stop":
                    if not running or target != launch.get("launch_id"):
                        code, payload = 409, {"error": "本次网页启动已结束或已切换"}
                    elif str(launch.get("workflow", "")).endswith("_recovery"):
                        code, payload = 409, {"error": "回执核对不可中断"}
                    else:
                        try:
                            update_stop_flag(launch_stop_flag(target), True)
                        except (OSError, SubmissionGateError):
                            code, payload = 503, {"error": "未能设置终止标记，请重试"}
                        else:
                            launch["stop_requested"] = True
                            code, payload = 202, dict(launch)
                elif running:
                    code, payload = 409, {"error": "请等待当前任务结束"}
                elif action == "resume":
                    _, saved = load_saved_campaign(root, target)
                    if not campaign_can_resume(saved):
                        code, payload = 409, {"error": "该 Campaign 没有可继续的研究批次"}
                    else:
                        selected = checked_selection({
                            "lead": request.get("lead"),
                            "executor": request.get("executor"),
                        }, agent_args, root)
                        pinned_path = log_dir / (target + ".models.json")
                        if pinned_path.exists():
                            previous = json.loads(pinned_path.read_text(encoding="utf-8"))
                            if previous != {"campaign_id": target, **selected}:
                                raise ValueError(
                                    "恢复时模型选择或注册配置与原 Campaign 不一致")
                        code, payload = start_campaign(
                            ["run-campaign", f"--resume-campaign={target}"],
                            credentials, selected, target, workflow="regular")
                else:
                    try:
                        with SingleWriterLock(root / "state" / "agent.lock"):
                            delete_saved_campaign(root, target)
                            (log_dir / (target + ".models.json")).unlink(missing_ok=True)
                    except (OSError, WriterLockError):
                        code, payload = 409, {"error": "研究状态正在使用或无法保存"}
                    else:
                        if launch.get("campaign_id") == target:
                            launch.clear()
                            launch["status"] = "idle"
                        code, payload = 200, {"deleted": target}
            self.respond_json(code, payload)

        def do_GET(self):
            if self.headers.get("Host") != f"127.0.0.1:{port}":
                self.send_error(403)
                return
            if self.path == "/":
                self.respond(200, "text/html; charset=utf-8",
                             (WORKBENCH / "AlphaResearchWorkbench.html").read_bytes())
                return
            if self.path == "/api/launch":
                with launch_lock:
                    payload = dict(launch)
                if "launch_id" in payload:
                    payload["events"] = recent_progress(log_dir / f"{payload['launch_id']}.jsonl")
                self.respond_json(200, payload)
                return
            if self.path == "/api/credentials":
                self.respond_json(200, {"configured": self.browser_credentials() is not None})
                return
            if self.path == "/api/models":
                try:
                    self.respond_json(200, listed_models(agent_args, root))
                except (OSError, ValueError):
                    self.respond_json(503, {"error": "模型注册中心不可用"})
                return
            if self.path == "/api/workbench-records":
                try:
                    self.respond_json(200, load_workbench_records(root))
                except (OSError, ValueError):
                    self.respond_json(503, {"error": "项目工作台记录不可读取"})
                return
            url = urlsplit(self.path)
            if url.path == "/api/research-summary":
                target = parse_qs(url.query).get("id", [""])[0]
                try:
                    path, _ = load_saved_campaign(root, target)
                    report = json.loads(path.with_name(f"{target}_summary.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    self.respond_json(404, {"error": "没有可读取的研究复盘"})
                else:
                    self.respond_json(200, report)
                return
            if self.path != "/api/status":
                self.send_error(404)
                return
            try:
                payload = json.dumps(read_status(agent_args), ensure_ascii=False).encode("utf-8")
            except (OSError, ValueError, subprocess.SubprocessError):
                self.respond(503, "application/json",
                             b'{"error":"Agent status unavailable"}')
                return
            self.respond(200, "application/json; charset=utf-8", payload)

    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765, choices=range(1, 65536),
                        metavar="PORT")
    parser.add_argument("--state-root")
    parser.add_argument("--config")
    parser.add_argument("--registry")
    args = parser.parse_args()
    agent_args = []
    for name in ("state_root", "config", "registry"):
        if value := getattr(args, name):
            agent_args.extend(["--" + name.replace("_", "-"), value])
    with ThreadingHTTPServer(("127.0.0.1", args.port),
                             handler_for(agent_args, args.port)) as server:
        print(f"Open http://127.0.0.1:{args.port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
