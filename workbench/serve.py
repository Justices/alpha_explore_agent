"""Serve the workbench and a read-only Agent status endpoint on loopback."""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys


WORKBENCH = Path(__file__).resolve().parent
PROJECT = WORKBENCH.parent


def read_status(agent_args):
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-B", "-m", "alpha_research", *agent_args, "status"],
        cwd=PROJECT,
        env=env,
        capture_output=True,
        check=True,
        encoding="utf-8",
        timeout=20,
    )
    return json.loads(result.stdout)


def handler_for(agent_args, port):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, code, content_type, body):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.headers.get("Host") != f"127.0.0.1:{port}":
                self.send_error(403)
                return
            if self.path == "/":
                self.respond(200, "text/html; charset=utf-8",
                             (WORKBENCH / "AlphaResearchWorkbench.html").read_bytes())
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
