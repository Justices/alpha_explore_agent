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

    def run_campaign(args, credentials, log_path):
        outcome = {"status": "failed"}
        stop_flag = launch_stop_flag(log_path.stem)
        try:
            write_progress("正在启动研究进程", log_path)
            result = subprocess.run(
                agent_command(agent_args, [*args, "--brain-credentials-stdin"]),
                cwd=PROJECT, env={**agent_env(), PROGRESS_ENV: str(log_path),
                                  "ALPHA_RESEARCH_LAUNCH_STOP": str(stop_flag)},
                input=json.dumps(credentials, ensure_ascii=False),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8")
            outcome["exit_code"] = result.returncode
            if result.returncode == 0:
                report = json.loads(result.stdout)
                if isinstance(report, dict) and isinstance(report.get("status"), str):
                    outcome["status"] = "exited"
                    outcome["campaign_status"] = report["status"]
                    if isinstance(report.get("campaign_id"), str):
                        outcome["campaign_id"] = report["campaign_id"]
        except (OSError, ValueError, subprocess.SubprocessError):
            write_progress("无法启动研究进程或读取执行结果", log_path)
        finally:
            if outcome["status"] == "failed" and stop_flag.exists() and outcome.get("exit_code") == 2:
                outcome.update(status="exited", campaign_status="user_stopped")
            if outcome["status"] == "failed":
                code = outcome.get("exit_code")
                write_progress(f"研究进程退出，退出码 {code}" if code is not None
                               else "研究进程启动失败", log_path)
            else:
                write_progress("研究进程已结束，结果见 Campaign 状态", log_path)
            with launch_lock:
                launch.update(outcome)
                try:
                    campaign_id = launch.get("campaign_id")
                    if campaign_id:
                        atomic_write_json(log_path.with_suffix(".campaign.json"),
                                          {"campaign_id": campaign_id})
                    stop_flag.unlink(missing_ok=True)
                except OSError:
                    write_progress("无法保存本次启动的索引或清理终止标记", log_path)

    def start_campaign(args, credentials, campaign_id=None):
        launch.clear()
        launch.update(status="running", launch_id=uuid4().hex)
        if campaign_id:
            launch["campaign_id"] = campaign_id
        log_path = log_dir / f"{launch['launch_id']}.jsonl"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path.touch(exist_ok=False)
            Thread(target=run_campaign, args=(args, credentials, log_path), daemon=True).start()
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
            if self.path not in {"/api/launch", "/api/credentials", "/api/research"}:
                self.send_error(404)
                return
            if self.headers.get_content_type() != "application/json":
                self.respond_json(415, {"error": "启动请求必须使用 JSON"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 16384:
                    raise ValueError("启动请求大小无效")
                self.connection.settimeout(5)
                request = json.loads(self.rfile.read(length))
                if self.path == "/api/credentials":
                    self.browser_credentials()  # Remove expired entries before storing credentials.
                    self.save_credentials(request)
                    return
                credentials = self.browser_credentials()
                if credentials is None:
                    self.respond_json(401, {"error": "请先设置 BRAIN 凭证，凭证有效期为 24 小时"})
                    return
                if self.path == "/api/research":
                    self.research_action(request, credentials)
                    return
                args = campaign_arguments(request)
            except (OSError, UnicodeError, json.JSONDecodeError):
                self.respond_json(400, {"error": "无法读取启动请求"})
                return
            except ValueError as exc:
                self.respond_json(400, {"error": str(exc)})
                return
            with launch_lock:
                if launch["status"] == "running":
                    code, payload = 409, {"error": "已有研究任务正在运行"}
                else:
                    code, payload = start_campaign(args, credentials)
            self.respond_json(code, payload)

        def research_action(self, request, credentials):
            if not isinstance(request, dict) or set(request) != {"action", "id"}:
                raise ValueError("需要研究操作和 ID")
            action, target = request["action"], request["id"]
            with launch_lock:
                running = launch["status"] == "running"
                if action == "stop":
                    if not running or target != launch.get("launch_id"):
                        code, payload = 409, {"error": "本次网页启动已结束或已切换"}
                    else:
                        try:
                            update_stop_flag(launch_stop_flag(target), True)
                        except (OSError, SubmissionGateError):
                            code, payload = 503, {"error": "未能设置终止标记，请重试"}
                        else:
                            launch["stop_requested"] = True
                            code, payload = 202, dict(launch)
                elif action not in ("resume", "delete"):
                    raise ValueError("研究操作无效")
                elif running:
                    code, payload = 409, {"error": "请等待当前任务结束"}
                elif action == "resume":
                    _, saved = load_saved_campaign(root, target)
                    if not campaign_can_resume(saved):
                        code, payload = 409, {"error": "该 Campaign 没有可继续的研究批次"}
                    else:
                        code, payload = start_campaign(
                            ["run-campaign", f"--resume-campaign={target}"], credentials, target)
                else:
                    try:
                        with SingleWriterLock(root / "state" / "agent.lock"):
                            delete_saved_campaign(root, target)
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
    args = parser.parse_args()
    agent_args = []
    for name in ("state_root", "config"):
        if value := getattr(args, name):
            agent_args.extend(["--" + name.replace("_", "-"), value])
    with ThreadingHTTPServer(("127.0.0.1", args.port),
                             handler_for(agent_args, args.port)) as server:
        print(f"Open http://127.0.0.1:{args.port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
