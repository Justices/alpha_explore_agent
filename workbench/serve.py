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
from uuid import uuid4


WORKBENCH = Path(__file__).resolve().parent
PROJECT = WORKBENCH.parent
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
    required = ("region", "objective", "max_batches")
    optional_text = ("category", "universe")
    if (not isinstance(data, dict)
            or not all(key in data for key in required)
            or set(data) - {*required, *optional_text, "dataset_ids", "delay", "ppa"}):
        raise ValueError("需要 Region、研究目标和批次预算")
    for key in ("region", "objective", *optional_text):
        if key in data and (not isinstance(data[key], str) or not data[key].strip()
                            or "\x00" in data[key]):
            raise ValueError(f"{key} 必须是非空文本")
    if type(data["max_batches"]) is not int or data["max_batches"] < 1:
        raise ValueError("批次预算必须是正整数")
    if "delay" in data and (type(data["delay"]) is not int or data["delay"] < 0):
        raise ValueError("Delay 必须是非负整数")
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
    for key in (*required, "category", "delay", "universe"):
        if key in data:
            args.append(f"--{key.replace('_', '-')}={data[key]}")
    args.extend(f"--dataset={dataset}" for dataset in datasets)
    if ppa:
        args.append("--ppa")
    return args


def handler_for(agent_args, port):
    origin = f"http://127.0.0.1:{port}"
    launch_lock = Lock()
    session_lock = Lock()
    sessions = {}
    launch = {"status": "idle"}

    def run_campaign(args, credentials):
        outcome = {"status": "failed"}
        try:
            result = subprocess.run(
                agent_command(agent_args, [*args, "--brain-credentials-stdin"]),
                cwd=PROJECT, env=agent_env(), input=json.dumps(credentials, ensure_ascii=False),
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
            pass
        finally:
            with launch_lock:
                launch.update(outcome)

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
            if self.path not in {"/api/launch", "/api/credentials"}:
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
                    launch.clear()
                    launch.update(status="running", launch_id=uuid4().hex)
                    try:
                        Thread(target=run_campaign, args=(args, credentials),
                               daemon=True).start()
                    except RuntimeError:
                        launch["status"] = "failed"
                        code, payload = 503, {"error": "无法启动研究进程"}
                    else:
                        code, payload = 202, dict(launch)
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
                self.respond_json(200, payload)
                return
            if self.path == "/api/credentials":
                self.respond_json(200, {"configured": self.browser_credentials() is not None})
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
