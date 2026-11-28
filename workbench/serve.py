"""Serve the local Alpha workbench and its guarded research workflows."""

import argparse
from datetime import datetime, timezone
import math
from http.cookies import CookieError, SimpleCookie
import json
import os
import re
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
                   for value in datasets)):
        raise ValueError("Dataset ID 必须是互不重复的非空文本")
    datasets = [value.strip() for value in datasets]
    if len(set(datasets)) != len(datasets):
        raise ValueError("Dataset ID 必须是互不重复的非空文本")
    ppa = data.get("ppa", False)
    if type(ppa) is not bool:
        raise ValueError("PPA Profile 必须显式启用")
    args = ["run-campaign"]
    for key in (*required, "max_batches", "category", "delay", "universe"):
        if key in data:
            value = data[key].strip() if key in (*required, *optional_text) else data[key]
            args.append(f"--{key.replace('_', '-')}={value}")
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


def plan_arguments(data):
    """Build a read-only BRAIN planning command without requiring model roles."""
    if not isinstance(data, dict):
        raise ValueError("预检请求必须是 JSON 对象")
    workflow = data.get("workflow", "regular")
    if workflow == "regular":
        allowed = {"workflow", "region", "dataset_ids", "category", "delay", "universe"}
        if set(data) - allowed:
            raise ValueError("Regular 预检参数包含不支持的字段")
        region = data.get("region")
        if not isinstance(region, str) or not region.strip() or "\x00" in region:
            raise ValueError("Region 必须是非空文本")
        delay = data.get("delay", 1)
        if type(delay) is not int or delay != 1:
            raise ValueError("Regular 预检必须使用 Delay 1")
        dataset_ids = data.get("dataset_ids")
        if dataset_ids is not None:
            if (not isinstance(dataset_ids, list) or not dataset_ids
                    or any(not isinstance(item, str) or not item.strip() or "\x00" in item
                           for item in dataset_ids)):
                raise ValueError("Dataset ID 必须是非空且不重复的列表")
            dataset_ids = [item.strip() for item in dataset_ids]
            if len(set(dataset_ids)) != len(dataset_ids):
                raise ValueError("Dataset ID 必须是非空且不重复的列表")
        args = ["plan-regular", f"--region={region.strip()}", "--delay=1"]
        if dataset_ids:
            for dataset_id in dataset_ids:
                args.append(f"--dataset={dataset_id}")
        for key in ("category", "universe"):
            value = data.get(key)
            if value is not None:
                if not isinstance(value, str) or not value.strip() or "\x00" in value:
                    raise ValueError(f"{key} 必须是非空文本")
                args.append(f"--{key}={value.strip()}")
        return workflow, args, None

    if workflow == "super":
        allowed = {"workflow", "region", "objective", "delay"}
        if set(data) - allowed:
            raise ValueError("Super 预检参数包含不支持的字段")
        for key in ("region", "objective"):
            value = data.get(key)
            if not isinstance(value, str) or not value.strip() or "\x00" in value:
                raise ValueError(f"{key} 必须是非空文本")
        args = [
            "plan-super", f"--region={data['region'].strip()}",
            f"--objective={data['objective'].strip()}",
        ]
        delay = data.get("delay")
        if delay is not None:
            if type(delay) is not int or delay < 0:
                raise ValueError("Super Delay 必须是非负整数")
            args.append(f"--delay={delay}")
        return workflow, args, None

    if workflow == "optimization":
        allowed = {"workflow", "alpha_id", "objective", "region"}
        if set(data) - allowed:
            raise ValueError("Alpha 来源预检参数包含不支持的字段")
        alpha_id = data.get("alpha_id")
        if not isinstance(alpha_id, str) or not alpha_id.strip() or "\x00" in alpha_id:
            raise ValueError("Alpha ID 必须是非空文本")
        args = ["plan-optimization", f"--alpha-id={alpha_id.strip()}"]
        objective = data.get("objective")
        if objective is not None:
            if not isinstance(objective, str) or not objective.strip() or "\x00" in objective:
                raise ValueError("优化目标必须是非空文本")
            args.append(f"--objective={objective.strip()}")
        region = data.get("region")
        if region is not None:
            if not isinstance(region, str) or not region.strip() or "\x00" in region:
                raise ValueError("Region 必须是非空文本")
            args.append(f"--region={region.strip()}")
        return workflow, args, alpha_id.strip()

    raise ValueError("不支持的预检工作流")


def _workbench_records_path(root):
    return Path(root) / "state" / "workbench" / "records.json"


def _validate_workbench_records(payload):
    if (not isinstance(payload, dict) or set(payload) != {"version", "cases", "alphas"}
            or type(payload.get("version")) is not int or payload.get("version") != 1
            or not isinstance(payload.get("cases"), list)
            or not isinstance(payload.get("alphas"), list)):
        raise ValueError("工作台记录格式无效")

    def required_text(item, key):
        value = item.get(key)
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError(f"工作台记录 {key} 缺失或无效")

    def optional_text(item, keys):
        for key in keys:
            value = item.get(key)
            if value is not None and (not isinstance(value, str) or "\x00" in value):
                raise ValueError(f"工作台记录 {key} 必须是文本")

    def finite_number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            return math.isfinite(float(value))
        except (OverflowError, ValueError):
            return False

    def nonnegative_integer(value):
        return finite_number(value) and value >= 0 and int(value) == value

    case_ids = set()
    for item in payload["cases"]:
        if not isinstance(item, dict):
            raise ValueError("研究任务记录必须是对象")
        required_text(item, "id")
        if item["id"] in case_ids:
            raise ValueError("研究任务 ID 重复")
        case_ids.add(item["id"])
        required_text(item, "name")
        if not isinstance(item.get("workflow"), str) or item["workflow"] not in {"regular", "super", "optimization"}:
            raise ValueError("研究任务工作流无效")
        if not isinstance(item.get("phase"), str) or item["phase"] not in {"scope", "hypothesis", "simulation", "review", "closed"}:
            raise ValueError("研究任务阶段无效")
        if item["workflow"] == "optimization":
            required_text(item, "sourceId")
        else:
            required_text(item, "region")
        optional_text(item, (
            "region", "dataset", "category", "universe", "objective", "constraints",
            "scopeEvidence", "coverage", "budget", "sourceId", "sourceType", "sourceSnapshot",
        ))
        if item.get("sourceType") is not None and item["sourceType"] not in {"regular", "super"}:
            raise ValueError("Alpha 来源类型无效")
        if "ppa" in item and type(item["ppa"]) is not bool:
            raise ValueError("PPA Profile 必须是布尔值")
        if item.get("delay") is not None and not nonnegative_integer(item["delay"]):
            raise ValueError("研究任务 Delay 必须是非负整数")
        history = item.get("history")
        if history is not None:
            if not isinstance(history, list):
                raise ValueError("研究阶段历史必须是列表")
            for entry in history:
                if (not isinstance(entry, dict)
                        or not isinstance(entry.get("phase"), str)
                        or entry["phase"] not in
                        {"scope", "hypothesis", "simulation", "review", "closed"}
                        or not isinstance(entry.get("at"), str)
                        or not entry["at"].strip()):
                    raise ValueError("研究阶段历史记录无效")
                try:
                    datetime.fromisoformat(entry["at"].replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValueError("研究阶段历史时间无效") from exc

    alpha_ids = set()
    metric_names = {
        "sharpe", "fitness", "turnover", "drawdown", "coverage", "robustness",
        "costs", "local", "prod", "self", "pairwise", "weight",
    }
    correlation_names = {"local", "prod", "self", "pairwise"}
    text_names = (
        "alphaId", "dataset", "category", "universe", "expression", "fields",
        "operators", "idea", "dataReason", "operatorReason", "economics", "settings",
        "receipt", "units", "raRef", "policyRef", "localRef", "corrRef", "pairRef",
        "auditRef", "preferredRef", "submissionNote",
    )
    status_values = {
        "raFinal": {"pass", "fail", "pending"},
        "localComplete": {"pass", "fail", "pending"},
        "pairwise": {"pass", "fail", "pending"},
        "auditStatus": {"candidate", "failed", "pending"},
        "preferredStatus": {"preferred", "failed", "pending"},
        "subUniverse": {"pass", "fail", "pending"},
    }
    for item in payload["alphas"]:
        if not isinstance(item, dict):
            raise ValueError("Alpha 记录必须是对象")
        required_text(item, "id")
        if item["id"] in alpha_ids:
            raise ValueError("Alpha 记录 ID 重复")
        alpha_ids.add(item["id"])
        required_text(item, "caseId")
        if item["caseId"] not in case_ids:
            raise ValueError("Alpha 记录引用了不存在的研究任务")
        required_text(item, "name")
        optional_text(item, text_names)
        metrics = item.get("metrics")
        if not isinstance(metrics, dict):
            raise ValueError("Alpha 指标必须是对象")
        for key, value in metrics.items():
            if key not in metric_names or not finite_number(value):
                raise ValueError("Alpha 指标名称或数值无效")
            if key in correlation_names and abs(value) > 1:
                raise ValueError("Alpha 相关性指标必须在 -1 到 1 之间")
        for key, allowed in status_values.items():
            if item.get(key) is not None and (not isinstance(item[key], str) or item[key] not in allowed):
                raise ValueError(f"Alpha {key} 状态无效")
        if item.get("corrLimit") is not None:
            value = item["corrLimit"]
            if not finite_number(value) or not 0 < value <= 1:
                raise ValueError("Alpha 相关性阈值必须在 0 到 1 之间")
        for key in ("delay", "raCount", "operatorCount"):
            value = item.get(key)
            if value is not None and not nonnegative_integer(value):
                raise ValueError(f"Alpha {key} 必须是非负整数")
        case = next(row for row in payload["cases"] if row["id"] == item["caseId"])
        if case["workflow"] == "regular":
            if case.get("dataset") and item.get("dataset"):
                allowed = [part.strip() for part in case["dataset"].replace("，", ",")
                           .replace("\n", ",").split(",") if part.strip()]
                if item["dataset"] not in allowed:
                    raise ValueError("Alpha Dataset 超出研究任务硬约束")
            for key in ("category", "delay", "universe"):
                if case.get(key) is not None and item.get(key) is not None and case[key] != item[key]:
                    raise ValueError(f"Alpha {key} 与研究任务硬约束冲突")

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


def launch_manifest_path(log_dir, launch_id):
    """Return the manifest path for a validated workbench launch identifier."""
    if not isinstance(launch_id, str) or re.fullmatch(r"[0-9a-f]{32}", launch_id) is None:
        raise ValueError("工作台启动 ID 无效")
    return Path(log_dir) / f"{launch_id}.launch.json"


def write_launch_manifest(log_dir, launch_id, manifest):
    """Atomically persist non-secret launch metadata and its terminal outcome."""
    directory = Path(log_dir)
    path = launch_manifest_path(directory, launch_id)
    if directory.is_symlink() or path.is_symlink():
        raise ValueError("工作台任务记录路径不能是符号链接")
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or path.is_symlink():
        raise ValueError("工作台任务记录路径不能是符号链接")
    payload = dict(manifest)
    payload["schema_version"] = 1
    payload["launch_id"] = launch_id
    atomic_write_json(path, payload)
    return payload


def read_launch_history(log_dir, limit=50, *, strict=False):
    """Read recent launch manifests; strict mode fails closed on unsafe history."""
    directory = Path(log_dir)
    if directory.is_symlink():
        if strict:
            raise ValueError("工作台任务历史目录不能是符号链接")
        return []
    try:
        directory.stat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        if strict:
            raise ValueError("无法读取工作台任务历史目录") from exc
        return []
    if not directory.is_dir():
        if strict:
            raise ValueError("工作台任务历史路径不是目录")
        return []
    records = []
    try:
        paths = list(directory.glob("*.launch.json"))
    except OSError as exc:
        if strict:
            raise ValueError("无法枚举工作台任务历史") from exc
        return []
    for path in paths:
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
                raise ValueError("任务清单文件类型或大小无效")
            launch_id = path.name[:-len(".launch.json")]
            if re.fullmatch(r"[0-9a-f]{32}", launch_id) is None:
                raise ValueError("任务清单文件名包含无效启动 ID")
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("任务清单必须是 JSON 对象")
            value.setdefault("launch_id", launch_id)
            if value["launch_id"] != launch_id:
                raise ValueError("任务清单 ID 与文件名不一致")
            value["_mtime_ns"] = path.stat().st_mtime_ns
            records.append(value)
        except (OSError, UnicodeError, ValueError) as exc:
            if strict:
                raise ValueError(f"工作台任务历史包含不可读取记录：{path.name}") from exc
            continue
    records.sort(key=lambda item: item["_mtime_ns"], reverse=True)
    return [{key: value for key, value in item.items() if key != "_mtime_ns"}
            for item in records[:max(1, min(int(limit), 200))]]


def restore_launch_state(log_dir):
    """Restore the latest launch; ambiguous in-flight work is blocked, never replayed."""
    try:
        history = read_launch_history(log_dir, limit=1, strict=True)
    except ValueError as exc:
        return {
            "status": "execution_unknown", "execution_unknown": True,
            "recovery_required": True, "manifest_history_unreadable": True,
            "recovery_message": (
                "工作台任务历史不可读取，无法证明旧 Simulation 是否结束。"
                "新任务已阻断；请检查 state-root 的任务清单文件，修复后再启动。"
            ),
        }
    if not history:
        return {"status": "idle"}
    latest = history[0]
    status = latest.get("status")
    if status in {"running", "starting"}:
        launch_id = latest["launch_id"]
        log_path = Path(log_dir) / f"{launch_id}.jsonl"
        events = recent_progress(log_path)
        last_message = events[-1].get("message", "") if events else ""
        if last_message.startswith(("研究进程已结束", "研究进程退出", "研究进程启动失败")):
            latest["status"] = "exited" if last_message.startswith("研究进程已结束") else "failed"
            latest["result_status"] = latest.get("result_status", "legacy_completion_observed")
            latest["recovered_from_legacy_log"] = True
        else:
            latest["status"] = "execution_unknown"
            latest["execution_unknown"] = True
            latest["recovery_required"] = True
            latest["recovery_message"] = (
                "工作台服务重启，无法证明旧子进程是否结束。为避免并发 Simulation，"
                "新任务已阻断；请先确认旧 Agent 子进程已退出。"
            )
        latest["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            write_launch_manifest(log_dir, launch_id, latest)
        except (OSError, ValueError):
            latest["manifest_persist_failed"] = True
            if latest.get("execution_unknown"):
                latest["recovery_message"] = (
                    "旧任务状态仍无法确认，且任务清单无法更新。为避免重复 Simulation，"
                    "新任务继续阻断；请检查 state-root 写入权限并人工核实旧进程。"
                )
    return latest


def recovery_intent_path(root, target):
    """Resolve only an existing, non-symlink Simulation Intent under state-root."""
    if not isinstance(target, str) or not target.strip():
        raise ValueError("Simulation Intent 路径无效")
    root = Path(root).resolve()
    path = Path(target).expanduser()
    if not path.is_absolute() or not path.name.endswith("_simulation_intent.json"):
        raise ValueError("仅允许核对状态目录内的 Simulation Intent 文件")
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Simulation Intent 位于当前 state-root 之外") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("Simulation Intent 路径包含非法目录段")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Simulation Intent 路径不能经过符号链接")
    if not current.is_file():
        raise ValueError("Simulation Intent 文件不存在或不可读取")
    resolved = current.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ValueError("Simulation Intent 位于当前 state-root 之外")
    return resolved


def handler_for(agent_args, port):
    origin = f"http://127.0.0.1:{port}"
    launch_lock = Lock()
    session_lock = Lock()
    sessions = {}
    root = (expand_path(agent_args[agent_args.index("--state-root") + 1]).resolve()
            if "--state-root" in agent_args else default_state_root())
    log_dir = root / "state" / "logs" / "workbench"
    launch = restore_launch_state(log_dir)

    def launch_stop_flag(launch_id):
        return root / "state" / f"workbench-{launch_id}.stop"

    def run_workflow(args, credentials, log_path, selection, workflow="regular"):
        outcome = {"status": "failed", "workflow": workflow}
        stop_flag = launch_stop_flag(log_path.stem)
        result = None

        def safe_diagnostic(value):
            text = str(value or "")
            secrets = [
                value for key, value in os.environ.items()
                if any(marker in key.upper()
                       for marker in ("KEY", "TOKEN", "PASSWORD", "SECRET", "CREDENTIAL"))
                and isinstance(value, str) and value
            ]
            secrets.extend(value for value in credentials.values()
                           if isinstance(value, str) and value)
            for secret in sorted(set(secrets), key=len, reverse=True):
                text = text.replace(secret, "[REDACTED]")
            return "\n".join(text.splitlines()[-20:])[:4000]

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
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", errors="replace")
            outcome["exit_code"] = result.returncode
            if result.returncode != 0:
                diagnostic = safe_diagnostic(result.stderr)
                if diagnostic:
                    write_progress(
                        "研究子进程错误输出（已脱敏，末尾最多 20 行）：\n" + diagnostic,
                        log_path)
            if result.returncode == 0:
                report = json.loads(result.stdout)
                if isinstance(report, dict):
                    report_status = report.get("status")
                    if workflow == "optimization_plan" and report.get("source_id"):
                        report_status = report_status or "source_captured"
                    if isinstance(report_status, str):
                        outcome["status"] = "exited"
                        outcome["result_status"] = report_status
                        if workflow == "regular":
                            outcome["campaign_status"] = report_status
                        for key in (
                            "campaign_id", "run_id", "branch_id", "branch_ref",
                            "source_alpha_id", "candidate_alpha_id", "candidate_id",
                            "candidate_ref", "preferred_ref", "alpha_id", "batch_id",
                            "iteration_session_id", "iterations_completed", "iteration_budget",
                            "iteration_stop_reason", "iteration_ref", "record_ref", "report_ref",
                            "simulation_intent_ref", "receipt_ref", "readback_ref", "audit_ref",
                            "candidate_status", "preferred_status", "proposal_sha256",
                            "candidate_definition_sha256", "policy_sha256",
                            "eligible_alpha_count", "unresolved_count",
                            "simulation_invoked", "alpha_submission_invoked",
                            "source_unchanged_verified", "plan_id", "source_id", "source_type",
                            "source_ref", "snapshot_ref", "details_sha256",
                            "source_properties_sha256", "coverage_complete", "dataset_count",
                            "combination_count", "requested", "allowed_delays", "region", "delay",
                        ):
                            value = report.get(key)
                            if isinstance(value, (str, int, float, bool, dict, list)):
                                outcome[key] = value
                        if workflow == "regular_plan":
                            datasets = report.get("datasets")
                            unresolved = report.get("unresolved")
                            if isinstance(datasets, list):
                                outcome["dataset_count"] = len(datasets)
                                outcome["combination_count"] = sum(
                                    len(item.get("combinations", []))
                                    for item in datasets if isinstance(item, dict)
                                    and isinstance(item.get("combinations"), list))
                            if isinstance(unresolved, list):
                                outcome["unresolved_count"] = len(unresolved)
                            if isinstance(report.get("plan_id"), str):
                                outcome["record_ref"] = str(
                                    root / "state" / "research_v1_1" / "plans"
                                    / f"{report['plan_id']}.json")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            diagnostic = safe_diagnostic(getattr(result, "stderr", ""))
            summary = f"研究进程异常：{type(exc).__name__}: {exc}"
            if diagnostic:
                summary += "\n诊断输出（已脱敏，末尾最多 20 行）：\n" + diagnostic
            write_progress(summary[:6000], log_path)
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
                    manifest = launch_manifest_path(log_dir, log_path.stem)
                    saved = json.loads(manifest.read_text(encoding="utf-8"))
                    saved.update(outcome)
                    saved["updated_at"] = datetime.now(timezone.utc).isoformat()
                    saved["finished_at"] = saved["updated_at"]
                    write_launch_manifest(log_dir, log_path.stem, saved)
                    campaign_id = launch.get("campaign_id")
                    if campaign_id:
                        atomic_write_json(log_path.with_suffix(".campaign.json"),
                                          {"campaign_id": campaign_id})
                        if outcome["status"] == "exited" and selection:
                            atomic_write_json(
                                log_dir / (campaign_id + ".models.json"), {
                                    "campaign_id": campaign_id, **selection})
                    stop_flag.unlink(missing_ok=True)
                except (OSError, ValueError):
                    write_progress("无法保存本次启动的索引或清理终止标记", log_path)

    def start_workflow(args, credentials, selection, campaign_id=None,
                       workflow="regular", primary_id=None):
        if launch.get("status") == "execution_unknown":
            return 409, {"error": launch.get("recovery_message",
                "旧任务执行状态未知；请先确认旧 Agent 子进程已退出。")}
        launch.clear()
        launch_id = uuid4().hex
        started_at = datetime.now(timezone.utc).isoformat()
        launch.update(status="running", launch_id=launch_id, workflow=workflow,
                      started_at=started_at, updated_at=started_at)
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
            write_launch_manifest(log_dir, launch_id, {
                "args": args, "models": selection, "workflow": workflow,
                "primary_id": primary_id, "status": "running",
                "started_at": started_at, "updated_at": started_at,
            })
            Thread(target=run_workflow,
                   args=(args, credentials, log_path, selection, workflow),
                   daemon=True).start()
        except (OSError, RuntimeError, ValueError):
            launch["status"] = "failed"
            launch["updated_at"] = datetime.now(timezone.utc).isoformat()
            try:
                write_launch_manifest(log_dir, launch_id, {
                    **launch, "args": args, "models": selection,
                    "primary_id": primary_id, "finished_at": launch["updated_at"],
                })
            except (OSError, ValueError):
                pass
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
                    "/api/workbench-records", "/api/plan"}:
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
                    try:
                        saved = save_workbench_records(root, request)
                    except WriterLockError:
                        self.respond_json(409, {"error": "工作台记录正在被另一个进程保存，请重试"})
                    else:
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
                if self.path == "/api/plan":
                    workflow, args, primary_id = plan_arguments(request)
                    selection = None
                else:
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
                if launch["status"] in {"running", "execution_unknown"}:
                    code, payload = 409, {"error": (
                        launch.get("recovery_message") if launch["status"] == "execution_unknown"
                        else "已有研究任务正在运行")}
                else:
                    code, payload = start_workflow(
                        args, credentials, selection,
                        workflow=f"{workflow}_plan" if self.path == "/api/plan" else workflow,
                        primary_id=primary_id)
            self.respond_json(code, payload)

        def research_action(self, request, credentials):
            if not isinstance(request, dict) or not {"action", "id"} <= set(request):
                raise ValueError("需要研究操作和 ID")
            action, target = request["action"], request["id"]
            if action == "recover":
                if set(request) != {"action", "id", "workflow"}:
                    raise ValueError("回执核对需要工作流和 Simulation Intent 路径")
                workflow = request.get("workflow")
                if not isinstance(workflow, str) or workflow not in {"super", "optimization"}:
                    raise ValueError("仅支持 Super 或 Alpha 优化回执核对")
                target_path = recovery_intent_path(root, target)
                with launch_lock:
                    if launch["status"] == "running":
                        code, payload = 409, {"error": "请等待当前任务结束"}
                    else:
                        code, payload = start_workflow(
                            ["recover-workflow", f"--workflow={workflow}", str(target_path)],
                            credentials, None, workflow=f"{workflow}_recovery",
                            primary_id=str(target_path))
                self.respond_json(code, payload)
                return

            if action == "resume":
                if set(request) != {"action", "id", "lead", "executor"}:
                    raise ValueError("恢复 Campaign 必须明确选择 Lead 与 Executor")
            elif action in {"stop", "delete", "acknowledge-interrupted"}:
                if set(request) != {"action", "id"}:
                    raise ValueError("研究操作参数无效")
            else:
                raise ValueError("研究操作无效")

            with launch_lock:
                running = launch["status"] == "running"
                if action == "acknowledge-interrupted":
                    if launch.get("status") != "execution_unknown" or target != launch.get("launch_id"):
                        code, payload = 409, {"error": "没有匹配的待核实任务"}
                    else:
                        acknowledged_at = datetime.now(timezone.utc).isoformat()
                        try:
                            saved = json.loads(launch_manifest_path(log_dir, target).read_text(encoding="utf-8"))
                            saved.update({
                                "status": "interrupted_acknowledged",
                                "execution_unknown": False,
                                "recovery_required": False,
                                "acknowledged_at": acknowledged_at,
                                "updated_at": acknowledged_at,
                            })
                            write_launch_manifest(log_dir, target, saved)
                        except (OSError, ValueError):
                            code, payload = 503, {"error": "无法保存人工核实记录"}
                        else:
                            launch.update({
                                "status": "interrupted_acknowledged",
                                "execution_unknown": False,
                                "recovery_required": False,
                                "acknowledged_at": acknowledged_at,
                                "updated_at": acknowledged_at,
                            })
                            code, payload = 200, dict(launch)
                elif action == "stop":
                    if not running or target != launch.get("launch_id"):
                        code, payload = 409, {"error": "本次网页启动已结束或已切换"}
                    elif str(launch.get("workflow", "")).endswith(("_recovery", "_plan")):
                        code, payload = 409, {"error": "只读核对任务不可中断"}
                    else:
                        try:
                            update_stop_flag(launch_stop_flag(target), True)
                        except (OSError, SubmissionGateError):
                            code, payload = 503, {"error": "未能设置终止标记，请重试"}
                        else:
                            stopped_at = datetime.now(timezone.utc).isoformat()
                            launch["stop_requested"] = True
                            launch["stop_requested_at"] = stopped_at
                            launch["updated_at"] = stopped_at
                            try:
                                saved = json.loads(
                                    launch_manifest_path(log_dir, target).read_text(encoding="utf-8"))
                                saved.update({
                                    "stop_requested": True,
                                    "stop_requested_at": stopped_at,
                                    "updated_at": stopped_at,
                                })
                                write_launch_manifest(log_dir, target, saved)
                            except (OSError, ValueError):
                                write_progress("终止标记已写入，但任务清单同步失败",
                                               log_dir / f"{target}.jsonl")
                            code, payload = 202, dict(launch)
                elif launch.get("status") == "execution_unknown":
                    code, payload = 409, {"error": launch.get("recovery_message",
                        "旧任务执行状态未知；请先确认旧 Agent 子进程已退出。")}
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
                        code, payload = start_workflow(
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
            if self.path == "/api/launches":
                self.respond_json(200, {"items": read_launch_history(log_dir, limit=50)})
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
