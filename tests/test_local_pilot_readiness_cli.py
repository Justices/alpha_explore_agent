"""Task-bound, read-only readiness for a separately authorized local Pilot."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from alpha_research.cli import main
from alpha_research.orchestration.engine import MainOrchestrator


class LocalPilotReadinessCliTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.prefix = [
            "--state-root", str(self.root),
            "--lead", "deepseek", "--executor", "deepseek",
        ]
        self.credentials = self.root / "test-credentials.json"
        self.credentials.write_text(json.dumps({
            "email": "offline@example.invalid", "password": "offline-only-fake",
        }), encoding="utf-8")

    def invoke(self, command, *, ready=True):
        output, errors = io.StringIO(), io.StringIO()
        with (patch.dict(os.environ, {
                  "DEEPSEEK_API_KEY": "offline-dummy" if ready else "",
                  "BRAIN_CREDENTIALS_FILE": (
                      str(self.credentials) if ready else ""),
              }),
              patch("alpha_research.cli.Gateway",
                    side_effect=AssertionError("BRAIN Gateway must not start")),
              patch("alpha_research.cli.build_model_client",
                    side_effect=AssertionError("no model factory allowed")),
              redirect_stdout(output), redirect_stderr(errors)):
            status = main(self.prefix + command)
        return status, output.getvalue(), errors.getvalue()

    def prepare(self, *, dataset=True):
        args = [
            "orchestration-prepare", "--workflow", "regular",
            "--objective", "local single-dataset acceptance",
            "--region", "USA", "--delay", "1", "--universe", "TOP3000",
        ]
        if dataset:
            args += ["--dataset", "analyst10"]
        code, text, error = self.invoke(args)
        self.assertEqual(code, 0, error)
        return json.loads(text)["task_id"]

    def test_local_prerequisites_ready_never_mean_simulation_authorized(self):
        task_id = self.prepare()
        before = MainOrchestrator(self.root).load(task_id)
        code, output, error = self.invoke([
            "orchestration-local-pilot-readiness", task_id])
        self.assertEqual(code, 0, error)
        report = json.loads(output)
        self.assertEqual(report["status"], "PILOT_LOCAL_READY_NOT_AUTHORIZED")
        self.assertEqual(report["blockers"], [])
        self.assertEqual(report["scope"]["dataset_ids"], ["analyst10"])
        self.assertEqual(report["scope"]["region"], "USA")
        self.assertEqual(report["local_checks"], {
            "config": "READY_LOCAL", "scope": "READY_LOCAL",
            "models": "READY_LOCAL", "model_budget": "READY_LOCAL",
            "brain_credentials": "READY_LOCAL",
        })
        for key in ("model_invoked", "brain_invoked", "brain_read_verified",
                    "simulation_authorized", "alpha_submission_authorized",
                    "live_dispatch_invoked"):
            self.assertFalse(report[key], key)
        self.assertTrue(report["explicit_user_authorization_required"])
        self.assertEqual(MainOrchestrator(self.root).load(task_id), before)

    def test_missing_local_credentials_lists_blockers_without_mutating_task(self):
        task_id = self.prepare()
        before = MainOrchestrator(self.root).load(task_id)
        code, output, error = self.invoke([
            "orchestration-local-pilot-readiness", task_id], ready=False)
        self.assertEqual(code, 2, error)
        report = json.loads(output)
        self.assertEqual(report["status"], "PILOT_LOCAL_BLOCKED")
        self.assertIn("local brain_credentials: BLOCKED", report["blockers"])
        self.assertTrue(any("models" in issue for issue in report["blockers"]))
        self.assertFalse(report["simulation_authorized"])
        self.assertEqual(MainOrchestrator(self.root).load(task_id), before)
        self.assertNotIn("offline-only-fake", output)

    def test_scope_drift_blocks_even_with_ready_local_credentials(self):
        task_id = self.prepare()
        config_path = self.root / "config" / "nastu_quant.json"
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        cfg["scope"]["universe"] = "TOP1000"
        cfg["scope"]["dataset_id"] = "analyst99"
        config_path.write_text(json.dumps(cfg), encoding="utf-8")
        before = MainOrchestrator(self.root).load(task_id)
        code, output, error = self.invoke([
            "orchestration-local-pilot-readiness", task_id])
        self.assertEqual(code, 2, error)
        report = json.loads(output)
        self.assertEqual(report["status"], "PILOT_LOCAL_BLOCKED")
        self.assertIn("configured universe conflicts with Task", report["blockers"])
        self.assertIn("configured dataset_id conflicts with Task", report["blockers"])
        self.assertEqual(report["local_checks"]["brain_credentials"], "READY_LOCAL")
        self.assertFalse(report["simulation_authorized"])
        self.assertEqual(MainOrchestrator(self.root).load(task_id), before)

    def test_regular_task_without_one_dataset_fails_pilot_safety_guard(self):
        task_id = self.prepare(dataset=False)
        code, output, error = self.invoke([
            "orchestration-local-pilot-readiness", task_id])
        self.assertEqual(code, 2, error)
        report = json.loads(output)
        self.assertEqual(report["status"], "PILOT_LOCAL_BLOCKED")
        self.assertTrue(any("exactly one pinned Dataset" in issue
                            for issue in report["blockers"]))
        self.assertFalse(report["live_dispatch_invoked"])
        self.assertEqual(MainOrchestrator(self.root).load(task_id)["status"],
                         "CREATED")


if __name__ == "__main__":
    unittest.main()
