import http.client
from contextlib import closing
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

from workbench import serve


class WorkbenchLaunchTests(unittest.TestCase):
    def test_launch_keeps_scope_and_rejects_cross_origin_and_duplicate_start(self):
        entered = threading.Event()
        release = threading.Event()
        commands = []
        child_credentials = []

        def run(command, **kwargs):
            commands.append(command)
            child_credentials.append(json.loads(kwargs["input"]))
            log_path = kwargs["env"].get("ALPHA_RESEARCH_PROGRESS_FILE")
            if log_path:
                with Path(log_path).open("a", encoding="utf-8") as stream:
                    stream.write('{"time":"2026-11-10T07:32:00Z","message":"正在规划字段诊断"}\n')
            entered.set()
            release.wait(5)
            return CompletedProcess(command, 0, '{"campaign_id":"case","status":"budget_complete"}')

        with TemporaryDirectory() as root, \
                patch.object(serve.subprocess, "run", side_effect=run), \
                patch.object(serve, "monotonic", create=True, return_value=100) as clock:
            server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
            port = server.server_address[1]
            server.RequestHandlerClass = serve.handler_for(["--state-root", root], port)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            request = {"region": "EUR", "objective": "--example", "max_batches": 2,
                       "dataset_ids": ["risk60", "model110"], "delay": 1,
                       "universe": "TOPCS1600", "ppa": True}
            body = json.dumps(request)

            def post(origin, data=body, *, path="/api/launch", cookie=""):
                with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
                    connection.request("POST", path, data, {
                        "Origin": origin, "Content-Type": "application/json", "Cookie": cookie})
                    response = connection.getresponse()
                    response.read()
                    return response.status, response.getheader("Set-Cookie")

            def status():
                with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
                    connection.request("GET", "/api/launch")
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    return json.loads(response.read())

            try:
                self.assertEqual(post("https://example.com")[0], 403)
                origin = f"http://127.0.0.1:{port}"
                credentials = {"username": "offline@example.invalid", "password": "offline-secret"}
                self.assertEqual(post(origin)[0], 401)
                code, header = post(origin, json.dumps(credentials), path="/api/credentials")
                self.assertEqual(code, 200)
                self.assertIn("Max-Age=86400", header)
                self.assertIn("HttpOnly", header)
                self.assertNotIn(credentials["password"], header)
                cookie = header.split(";", 1)[0]
                self.assertEqual(post(origin, '{"region":"EUR"}', cookie=cookie)[0], 400)
                self.assertEqual(post(origin, json.dumps({**request, "delay": 0}), cookie=cookie)[0], 400)
                self.assertEqual(post(origin, cookie=cookie)[0], 202)
                self.assertTrue(entered.wait(3))
                running = status()
                self.assertEqual(running["status"], "running")
                self.assertIn("正在规划字段诊断", [event["message"] for event in running["events"]])
                self.assertEqual(post(origin, cookie=cookie)[0], 409)
                self.assertEqual(len(commands), 1)
                self.assertEqual(child_credentials, [credentials])
                self.assertEqual(commands[0][4:], [
                    "--state-root", root, "run-campaign", "--region=EUR",
                    "--objective=--example", "--max-batches=2",
                    "--delay=1", "--universe=TOPCS1600",
                    "--dataset=risk60", "--dataset=model110", "--ppa",
                    "--brain-credentials-stdin"])
            finally:
                release.set()
            deadline = time.monotonic() + 3
            report = status()
            while report["status"] == "running" and time.monotonic() < deadline:
                report = status()
            self.assertEqual(report["status"], "exited")
            self.assertEqual(report["campaign_status"], "budget_complete")
            self.assertEqual(report["campaign_id"], "case")
            self.assertIn("正在规划字段诊断", [event["message"] for event in report["events"]])
            self.assertNotIn(credentials["password"], json.dumps(report))
            self.assertNotIn("password", report)
            clock.return_value = 86500
            self.assertEqual(post(origin, cookie=cookie)[0], 401)
            self.assertEqual(len(commands), 1)
