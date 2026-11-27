import http.client
from contextlib import closing, redirect_stdout
from http.server import ThreadingHTTPServer
from io import StringIO
from types import SimpleNamespace
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch

from alpha_research.cli_status import run_status_command
from alpha_research.config import DEFAULT_CONFIG
from alpha_research.qualification.pool import policy_hash
from workbench import serve


class WorkbenchArgumentTests(unittest.TestCase):
    def test_regular_arguments_preserve_user_scope_and_ppa_profile(self):
        workflow, args, primary = serve.research_arguments({
            "workflow": "regular", "region": "USA", "objective": "quality",
            "dataset_ids": ["analyst10", "fundamental6"], "category": "Financial",
            "delay": 1, "universe": "TOP3000", "max_batches": 2, "ppa": True,
        })
        self.assertEqual(workflow, "regular")
        self.assertIsNone(primary)
        self.assertEqual(args, [
            "run-campaign", "--region=USA", "--objective=quality",
            "--max-batches=2", "--category=Financial", "--delay=1",
            "--universe=TOP3000", "--dataset=analyst10",
            "--dataset=fundamental6", "--ppa",
        ])

    def test_super_arguments_are_bounded_and_keep_native_scope(self):
        workflow, args, primary = serve.research_arguments({
            "workflow": "super", "region": "USA", "objective": "selection research",
            "delay": 1, "max_iterations": 4,
        })
        self.assertEqual(workflow, "super")
        self.assertIsNone(primary)
        self.assertEqual(args, [
            "run-super", "--region=USA", "--objective=selection research",
            "--max-iterations=4", "--delay=1",
        ])

    def test_optimization_requires_source_alpha_and_returns_it_as_primary_id(self):
        workflow, args, primary = serve.research_arguments({
            "workflow": "optimization", "alpha_id": "YPaepL9o",
            "objective": "reduce turnover", "region": "USA", "max_iterations": 3,
        })
        self.assertEqual(workflow, "optimization")
        self.assertEqual(primary, "YPaepL9o")
        self.assertEqual(args, [
            "run-optimization", "--alpha-id=YPaepL9o",
            "--objective=reduce turnover", "--max-iterations=3", "--region=USA",
        ])

    def test_invalid_workflow_budgets_and_unknown_fields_are_rejected(self):
        invalid = [
            {"workflow": "super", "region": "USA", "objective": "x", "max_iterations": 0},
            {"workflow": "optimization", "alpha_id": "A", "objective": "x", "max_iterations": 21},
            {"workflow": "optimization", "alpha_id": "A", "objective": "x", "surprise": True},
            {"workflow": "super", "region": "USA", "objective": "x", "delay": -1},
            {"workflow": "unknown", "region": "USA", "objective": "x"},
        ]
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ValueError):
                serve.research_arguments(request)


class WorkbenchPlanTests(unittest.TestCase):
    def test_regular_plan_keeps_only_explicit_scope_constraints(self):
        workflow, args, primary = serve.plan_arguments({
            "workflow": "regular", "region": "USA",
            "dataset_ids": ["analyst10", "fundamental6"],
            "category": "Financial", "delay": 1, "universe": "TOP3000",
        })
        self.assertEqual(workflow, "regular")
        self.assertIsNone(primary)
        self.assertEqual(args, [
            "plan-regular", "--region=USA", "--delay=1",
            "--dataset=analyst10", "--dataset=fundamental6",
            "--category=Financial", "--universe=TOP3000",
        ])

    def test_super_plan_uses_native_platform_selection_preflight(self):
        workflow, args, primary = serve.plan_arguments({
            "workflow": "super", "region": "USA",
            "objective": "selection research", "delay": 1,
        })
        self.assertEqual(workflow, "super")
        self.assertIsNone(primary)
        self.assertEqual(args, [
            "plan-super", "--region=USA",
            "--objective=selection research", "--delay=1",
        ])

    def test_optimization_plan_pins_the_source_alpha(self):
        workflow, args, primary = serve.plan_arguments({
            "workflow": "optimization", "alpha_id": "YPaepL9o",
            "objective": "reduce turnover", "region": "USA",
        })
        self.assertEqual(workflow, "optimization")
        self.assertEqual(primary, "YPaepL9o")
        self.assertEqual(args, [
            "plan-optimization", "--alpha-id=YPaepL9o",
            "--objective=reduce turnover", "--region=USA",
        ])

    def test_preflight_rejects_ambiguous_or_unsupported_scope(self):
        invalid = [
            {"workflow": "regular", "region": "USA", "dataset_ids": []},
            {"workflow": "regular", "region": "USA",
             "dataset_ids": ["analyst10", "analyst10"]},
            {"workflow": "regular", "region": "USA", "delay": 0},
            {"workflow": "super", "region": "USA", "objective": "x",
             "dataset_ids": ["analyst10"]},
            {"workflow": "optimization", "alpha_id": "A", "surprise": True},
        ]
        for request in invalid:
            with self.subTest(request=request), self.assertRaises(ValueError):
                serve.plan_arguments(request)


class WorkbenchRecoveryPathTests(unittest.TestCase):
    def test_recovery_accepts_only_existing_intents_inside_state_root(self):
        with TemporaryDirectory() as root, TemporaryDirectory() as outside:
            intent = Path(root) / "state" / "research_v1_1" / "optimization" / "run_simulation_intent.json"
            intent.parent.mkdir(parents=True)
            intent.write_text('{"status":"POSTING"}', encoding="utf-8")
            self.assertEqual(serve.recovery_intent_path(root, str(intent)), intent)
            arbitrary = intent.with_name("report.json")
            arbitrary.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                serve.recovery_intent_path(root, str(arbitrary))
            foreign = Path(outside) / "run_simulation_intent.json"
            foreign.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "之外"):
                serve.recovery_intent_path(root, str(foreign))
            with self.assertRaises(ValueError):
                serve.recovery_intent_path(root, str(intent.parent / ".." / intent.name))


class WorkbenchStatusPolicyTests(unittest.TestCase):
    def test_status_keeps_unqualified_candidate_and_preferred_evaluations_visible(self):
        with TemporaryDirectory() as root:
            root_path = Path(root)
            config = root_path / "config" / "nastu_quant.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"scope": {"region": "USA"}}), encoding="utf-8")
            candidate_dir = root_path / "state" / "research_v1_1" / "candidate"
            preferred_dir = root_path / "state" / "research_v1_1" / "preferred"
            candidate_dir.mkdir(parents=True)
            preferred_dir.mkdir(parents=True)
            (candidate_dir / "candidate-1.json").write_text(json.dumps({
                "candidate_id": "candidate-1", "alpha_id": "ALPHA-1",
                "workflow": "super", "status": "RA_UNAVAILABLE",
                "failures": [], "pending": ["ra_checks_unsettled_or_unavailable"],
                "metrics": {"sharpe": 1.25}, "gate_evidence": {"ra_fail_count": None},
                "updated_at": "2026-11-27T10:00:00+00:00",
            }), encoding="utf-8")
            (preferred_dir / "candidate-1.json").write_text(json.dumps({
                "candidate_id": "candidate-1", "alpha_id": "ALPHA-1",
                "workflow": "super", "status": "NOT_PREFERRED",
                "qualified": False, "pending": ["candidate_not_qualified"],
                "updated_at": "2026-11-27T10:00:00+00:00",
            }), encoding="utf-8")
            output = StringIO()
            with patch("alpha_research.cli_status.CandidatePool.build_index",
                       return_value={"status": "available", "entries": []}):
                with redirect_stdout(output):
                    self.assertEqual(run_status_command(
                        SimpleNamespace(config=config), root_path,
                        root_path / "state" / "stop.flag"), 0)
            report = json.loads(output.getvalue())
            self.assertEqual(len(report["v11_candidate_evaluations"]), 1)
            candidate = report["v11_candidate_evaluations"][0]
            self.assertEqual(candidate["status"], "RA_UNAVAILABLE")
            self.assertEqual(candidate["pending"], ["ra_checks_unsettled_or_unavailable"])
            self.assertEqual(candidate["metrics"]["sharpe"], 1.25)
            self.assertEqual(len(report["v11_preferred_evaluations"]), 1)
            self.assertEqual(report["v11_preferred_evaluations"][0]["status"], "NOT_PREFERRED")
            self.assertEqual(report["v11_candidates"], [])
            self.assertEqual(report["v11_preferred"], [])

    def test_status_exposes_canonical_application_owned_policy(self):
        with TemporaryDirectory() as root:
            config = Path(root) / "config" / "nastu_quant.json"
            config.parent.mkdir(parents=True)
            config.write_text(json.dumps({"scope": {"region": "USA"}}),
                              encoding="utf-8")
            output = StringIO()
            args = SimpleNamespace(config=config)
            with patch("alpha_research.cli_status.CandidatePool.build_index",
                       return_value={"status": "available", "entries": []}), \
                    redirect_stdout(output):
                self.assertEqual(
                    run_status_command(args, Path(root), Path(root) / "state" / "stop.flag"),
                    0)
            report = json.loads(output.getvalue())
            policy = report["qualification_policy"]
            self.assertEqual(policy["status"], "available")
            self.assertEqual(policy["candidate"], DEFAULT_CONFIG["gate"])
            self.assertEqual(policy["preferred"], DEFAULT_CONFIG["preferred"])
            self.assertEqual(policy["policy_sha256"], policy_hash(DEFAULT_CONFIG))


class WorkbenchRecordStoreTests(unittest.TestCase):
    def test_project_records_round_trip_through_locked_store(self):
        with TemporaryDirectory() as root:
            payload = {
                "version": 1,
                "cases": [{
                    "id": "case-1", "name": "USA Regular", "workflow": "regular",
                    "region": "USA", "phase": "scope", "ppa": False,
                }],
                "alphas": [{
                    "id": "alpha-1", "caseId": "case-1", "name": "Analyst Alpha",
                    "metrics": {},
                }],
            }
            result = serve.save_workbench_records(root, payload)
            self.assertEqual(result, {
                "saved": True, "case_count": 1, "alpha_count": 1,
            })
            self.assertEqual(serve.load_workbench_records(root), payload)

    def test_project_records_reject_duplicate_ids_and_non_finite_values(self):
        valid_case = {
            "id": "same", "name": "USA Regular", "workflow": "regular",
            "region": "USA", "phase": "scope", "ppa": False,
        }
        duplicate = {
            "version": 1,
            "cases": [valid_case, dict(valid_case)],
            "alphas": [],
        }
        non_finite = {
            "version": 1,
            "cases": [{
                "id": "case-1", "name": "USA Regular", "workflow": "regular",
                "region": "USA", "phase": "scope", "metric": float("nan"),
            }],
            "alphas": [],
        }
        with self.assertRaisesRegex(ValueError, "重复"):
            serve._validate_workbench_records(duplicate)
        with self.assertRaisesRegex(ValueError, "无法保存"):
            serve._validate_workbench_records(non_finite)


class WorkbenchPlanEndpointTests(unittest.TestCase):
    def test_read_only_preflight_does_not_require_model_roles(self):
        entered = threading.Event()
        commands = []
        child_credentials = []

        def run(command, **kwargs):
            commands.append(command)
            child_credentials.append(json.loads(kwargs["input"]))
            entered.set()
            return CompletedProcess(command, 0, json.dumps({
                "status": "planned", "plan_id": "plan-123",
                "datasets": [{"combinations": [{}, {}]}], "unresolved": [],
            }))

        with TemporaryDirectory() as root, \
                patch.object(serve.subprocess, "run", side_effect=run):
            server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
            port = server.server_address[1]
            server.RequestHandlerClass = serve.handler_for(["--state-root", root], port)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            origin = f"http://127.0.0.1:{port}"

            def post(path, payload, cookie=""):
                with closing(http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=5)) as connection:
                    connection.request("POST", path, json.dumps(payload), {
                        "Origin": origin, "Content-Type": "application/json",
                        "Cookie": cookie,
                    })
                    response = connection.getresponse()
                    body = json.loads(response.read())
                    return response.status, response.getheader("Set-Cookie"), body

            status, cookie_header, _ = post("/api/credentials", {
                "username": "offline@example.invalid", "password": "offline-secret",
            })
            self.assertEqual(status, 200)
            cookie = cookie_header.split(";", 1)[0]
            status, _, launched = post("/api/plan", {
                "workflow": "regular", "region": "USA",
                "dataset_ids": ["analyst10"], "delay": 1,
            }, cookie)
            self.assertEqual(status, 202)
            self.assertTrue(entered.wait(3))

            deadline = time.monotonic() + 3
            report = None
            while time.monotonic() < deadline:
                with closing(http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=5)) as connection:
                    connection.request("GET", "/api/launch")
                    response = connection.getresponse()
                    report = json.loads(response.read())
                if report["status"] != "running":
                    break
                time.sleep(0.01)

            self.assertEqual(report["status"], "exited")
            self.assertEqual(report["workflow"], "regular_plan")
            self.assertEqual(report["plan_id"], "plan-123")
            self.assertEqual(report["dataset_count"], 1)
            self.assertEqual(report["combination_count"], 2)
            self.assertEqual(child_credentials, [{
                "username": "offline@example.invalid", "password": "offline-secret",
            }])
            self.assertIn("plan-regular", commands[0])
            self.assertNotIn("--lead", commands[0])
            self.assertNotIn("--executor", commands[0])
            self.assertNotIn("run-campaign", commands[0])
            self.assertEqual(launched["workflow"], "regular_plan")


class WorkbenchFrontendSyntaxTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is not installed")
    def test_inline_workbench_javascript_parses(self):
        html_path = Path(__file__).resolve().parent.parent / "workbench" / "AlphaResearchWorkbench.html"
        html = html_path.read_text(encoding="utf-8")
        scripts = re.findall(r"<script\b[^>]*>(.*?)</script\s*>", html, re.IGNORECASE | re.DOTALL)
        self.assertTrue(scripts, "Workbench must contain an inline application script")
        with TemporaryDirectory() as root:
            for index, source in enumerate(scripts):
                script_path = Path(root) / f"workbench-{index}.js"
                script_path.write_text(source, encoding="utf-8")
                result = subprocess.run(
                    ["node", "--check", str(script_path)],
                    capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)


class WorkbenchLaunchPersistenceTests(unittest.TestCase):
    def test_inflight_launch_is_restored_as_unknown_and_blocks_replay(self):
        with TemporaryDirectory() as root:
            log_dir = Path(root) / "state" / "logs" / "workbench"
            launch_id = "a" * 32
            serve.write_launch_manifest(log_dir, launch_id, {
                "workflow": "super", "status": "running",
                "started_at": "2026-11-27T10:00:00+00:00",
                "args": ["run-super"], "models": None,
            })

            restored = serve.restore_launch_state(log_dir)

            self.assertEqual(restored["launch_id"], launch_id)
            self.assertEqual(restored["status"], "execution_unknown")
            self.assertTrue(restored["recovery_required"])
            self.assertIn("避免并发 Simulation", restored["recovery_message"])
            persisted = json.loads(
                (log_dir / f"{launch_id}.launch.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "execution_unknown")
            self.assertEqual(serve.read_launch_history(log_dir)[0]["launch_id"], launch_id)

    def test_legacy_terminal_progress_is_not_misclassified_as_running(self):
        with TemporaryDirectory() as root:
            log_dir = Path(root) / "state" / "logs" / "workbench"
            launch_id = "b" * 32
            serve.write_launch_manifest(log_dir, launch_id, {
                "workflow": "regular", "status": "starting",
                "started_at": "2026-11-27T10:00:00+00:00",
            })
            (log_dir / f"{launch_id}.jsonl").write_text(
                '{"time":"2026-11-27T10:01:00+00:00","message":"研究进程已结束，结果见 Agent 状态与证据记录"}\n',
                encoding="utf-8")

            restored = serve.restore_launch_state(log_dir)

            self.assertEqual(restored["status"], "exited")
            self.assertTrue(restored["recovered_from_legacy_log"])
            self.assertEqual(restored["result_status"], "legacy_completion_observed")

    def test_launch_manifest_rejects_untrusted_identifiers(self):
        with TemporaryDirectory() as root:
            for launch_id in ("../escape", "A" * 32, "", "not-a-uuid"):
                with self.subTest(launch_id=launch_id), self.assertRaises(ValueError):
                    serve.launch_manifest_path(root, launch_id)


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
                patch.dict(os.environ, {"ZAI_API_KEY": "offline-fixture",
                                         "MOONSHOT_API_KEY": "offline-fixture"}), \
                patch.object(serve.subprocess, "run", side_effect=run), \
                patch.object(serve, "monotonic", create=True, return_value=100) as clock:
            cfg = Path(root) / "config" / "nastu_quant.json"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(json.dumps({
                "model_governance": {
                    "max_calls": 5, "max_reported_tokens": 1000,
                    "failover": "manual",
                },
            }), encoding="utf-8")
            server = ThreadingHTTPServer(("127.0.0.1", 0), serve.BaseHTTPRequestHandler)
            port = server.server_address[1]
            server.RequestHandlerClass = serve.handler_for(["--state-root", root], port)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            request = {"lead": "glm", "executor": "kimi",
                       "region": "EUR", "objective": "--example",
                       "dataset_ids": ["risk60", "model110"], "delay": 1,
                       "universe": "TOPCS1600", "ppa": True}
            body = json.dumps(request)
            saved_id = "a" * 32
            saved_path = Path(root) / "state/research_v1_1/campaign_runs" / f"{saved_id}.json"
            saved_path.parent.mkdir(parents=True)
            saved_path.write_text(json.dumps({
                "campaign_id": saved_id, "status": "user_stopped", "active_item": 0,
            }), encoding="utf-8")
            batch = Path(root) / "runs/EUR/D1/TOPCS1600/risk60/batch1"
            batch.mkdir(parents=True)
            (batch / "batch_context.json").write_text(
                json.dumps({"research_id": f"regular:campaign:{saved_id}"}), encoding="utf-8")
            intent = batch / "simulation_intent.json"
            intent.write_text('{"status":"SUBMITTING"}', encoding="utf-8")
            summary = saved_path.with_name(f"{saved_id}_summary.json")
            summary.write_text(json.dumps({"campaign_id": saved_id}), encoding="utf-8")

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
                with closing(http.client.HTTPConnection(
                        "127.0.0.1", port, timeout=5)) as connection:
                    connection.request("GET", "/api/models")
                    response = connection.getresponse()
                    models = json.loads(response.read())
                    self.assertEqual(response.status, 200)
                    self.assertIn("glm", {row["id"] for row in models["entries"]})
                    self.assertFalse(models["provider_invoked"])
                    self.assertNotIn("offline-secret", json.dumps(models))
                self.assertEqual(post(origin, '{"region":"EUR"}', cookie=cookie)[0], 400)
                self.assertEqual(post(origin, json.dumps({
                    key: value for key, value in request.items() if key != "executor"
                }), cookie=cookie)[0], 400)
                self.assertEqual(post(origin, json.dumps({
                    **request, "lead": "unregistered"
                }), cookie=cookie)[0], 400)
                self.assertEqual(post(origin, json.dumps({**request, "delay": 0}), cookie=cookie)[0], 400)
                self.assertEqual(post(origin, cookie=cookie)[0], 202)
                self.assertTrue(entered.wait(3))
                running = status()
                self.assertEqual(running["status"], "running")
                self.assertIn("正在规划字段诊断", [event["message"] for event in running["events"]])
                self.assertEqual(post(origin, cookie=cookie)[0], 409)
                def action(name, target, session=cookie, **model_override):
                    payload = {"action": name, "id": target}
                    if name == "resume":
                        payload.update(lead="glm", executor="kimi")
                    payload.update(model_override)
                    return post(origin, json.dumps(payload),
                                path="/api/research", cookie=session)[0]

                self.assertEqual(action("stop", "stale-launch"), 409)
                self.assertEqual(action("delete", saved_id), 409)
                self.assertEqual(action("stop", running["launch_id"]), 202)
                launch_flag = Path(root) / "state" / f"workbench-{running['launch_id']}.stop"
                self.assertTrue(launch_flag.exists())
                self.assertFalse((Path(root) / "state/stop.flag").exists())
                self.assertTrue(status()["stop_requested"])
                self.assertEqual(len(commands), 1)
                self.assertEqual(child_credentials, [credentials])
                metadata = Path(root) / "state/logs/workbench" / f"{running['launch_id']}.launch.json"
                recorded = json.loads(metadata.read_text(encoding="utf-8"))
                self.assertEqual(recorded["args"][0], "run-campaign")
                self.assertEqual(recorded["models"]["lead"], "glm")
                self.assertEqual(recorded["models"]["executor"], "kimi")
                self.assertEqual(len(recorded["models"]["profile_sha256"]), 64)
                self.assertIn("--region=EUR", recorded["args"])
                self.assertNotIn(credentials["password"], metadata.read_text(encoding="utf-8"))
                self.assertEqual(commands[0][4:], [
                    "--state-root", root, "--lead", "glm", "--executor", "kimi",
                    "run-campaign", "--region=EUR",
                    "--objective=--example",
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
            manifest_path = Path(root) / "state/logs/workbench" / f"{report['launch_id']}.launch.json"
            persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["status"], "exited")
            self.assertEqual(persisted["campaign_status"], "budget_complete")
            with closing(http.client.HTTPConnection("127.0.0.1", port, timeout=5)) as connection:
                connection.request("GET", "/api/launches")
                response = connection.getresponse()
                history = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertIn(report["launch_id"], {item["launch_id"] for item in history["items"]})
            self.assertFalse(launch_flag.exists())
            entered.clear()
            self.assertEqual(action("resume", saved_id, ""), 401)
            pinned = Path(root) / "state/logs/workbench" / f"{saved_id}.models.json"
            pinned.write_text(json.dumps({
                "campaign_id": saved_id, **recorded["models"]}), encoding="utf-8")
            self.assertEqual(action("resume", saved_id, lead="kimi"), 400)
            self.assertEqual(len(commands), 1)
            self.assertEqual(action("resume", saved_id), 202)
            self.assertTrue(entered.wait(3))
            self.assertEqual(commands[-1][10:], [
                "run-campaign", f"--resume-campaign={saved_id}", "--brain-credentials-stdin"])
            deadline = time.monotonic() + 3
            while status()["status"] == "running" and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(action("delete", saved_id), 400)
            self.assertTrue(batch.exists())
            intent.write_text('{"status":"READ_BACK"}', encoding="utf-8")
            self.assertEqual(action("delete", saved_id), 200)
            self.assertFalse(saved_path.exists())
            self.assertFalse(summary.exists())
            self.assertFalse(batch.exists())
            self.assertEqual(action("resume", saved_id), 400)
            clock.return_value = 86500
            self.assertEqual(post(origin, cookie=cookie)[0], 401)
            self.assertEqual(len(commands), 2)
