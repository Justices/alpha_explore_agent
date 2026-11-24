"""CLI P2 prepared task: synthetic-only state machine and immutable identity."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from alpha_research.cli import main
from alpha_research.orchestration.engine import MainOrchestrator
from alpha_research.config import DEFAULT_CONFIG


class PreparedMockCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.args = ["--state-root", str(self.root), "--lead", "deepseek",
                     "--executor", "deepseek"]

    def call(self, args):
        out, err = io.StringIO(), io.StringIO()
        with (patch("alpha_research.cli.Gateway",
                    side_effect=AssertionError("external Gateway is forbidden")),
              patch("alpha_research.cli.build_model_client",
                    side_effect=AssertionError("provider model is forbidden")),
              patch("alpha_research.orchestration.dispatch.dispatch",
                    side_effect=AssertionError("domain dispatch is forbidden")),
              redirect_stdout(out), redirect_stderr(err)):
            status = main(args)
        return status, out.getvalue(), err.getvalue()

    def prepare(self, workflow):
        args = self.args + [
            "orchestration-prepare", "--workflow", workflow,
            "--region", "CHN", "--objective", "CLI synthetic test",
        ]
        if workflow == "optimization":
            args += ["--alpha-id", "OFFLINE_SOURCE"]
        status, out, err = self.call(args)
        self.assertEqual(status, 0, err)
        return json.loads(out)["task_id"]

    def test_regular_super_optimization_prepared_run_with_no_external_work(self):
        for workflow in ("regular", "super", "optimization"):
            with self.subTest(workflow=workflow):
                task_id = self.prepare(workflow)
                status, out, err = self.call(
                    self.args + ["orchestration-run", task_id, "--mode", "mock"])
                self.assertEqual(status, 0, err)
                result = json.loads(out)
                self.assertEqual(result["status"], "COMPLETED")
                self.assertEqual(result["mode"], "synthetic_fixture")
                self.assertFalse(result["domain_dispatch_invoked"])
                self.assertFalse(result["brain_invoked"])
                self.assertFalse(result["model_invoked"])
                task = MainOrchestrator(self.root).load(task_id)
                self.assertEqual(task["budget"]["used"], 1)
                self.assertEqual(task["budget"]["reserved"], 0)
                self.assertTrue(all(value == "DONE"
                                    for value in task["steps"].values()))
                self.assertEqual(task["result"]["status"], "SYNTHETIC_ONLY")
                self.assertFalse(task["result"]["candidate_record_created"])
                self.assertEqual(list(self.root.rglob("simulation_intent.json")), [])
                self.assertEqual(list(self.root.rglob("model_budget/*.json")), [])

    def test_completed_task_cannot_be_replayed(self):
        task_id = self.prepare("super")
        self.assertEqual(
            self.call(self.args + ["orchestration-run", task_id, "--mode", "mock"])[0], 0)
        status, _, error = self.call(
            self.args + ["orchestration-run", task_id, "--mode", "mock"])
        self.assertEqual(status, 2)
        self.assertIn("OrchestrationError", error)
        self.assertEqual(MainOrchestrator(self.root).load(task_id)["budget"]["used"], 1)

    def test_user_policy_override_is_normalized_before_prepared_run(self):
        task_id = self.prepare("regular")
        config = self.root / "custom.json"
        config.write_text(json.dumps({
            "scope": {"region": "CHN", "delay": 1},
            "model_governance": {
                "max_calls": 2, "max_reported_tokens": 4096, "failover": "manual"},
        }), encoding="utf-8")
        status, out, error = self.call(
            self.args[:2] + ["--config", str(config)] + self.args[2:]
            + ["orchestration-run", task_id, "--mode", "mock"])
        self.assertEqual(status, 0, error)
        self.assertEqual(json.loads(out)["status"], "COMPLETED")
        self.assertEqual(json.loads(config.read_text(encoding="utf-8"))["model_governance"],
                         DEFAULT_CONFIG["model_governance"])
        self.assertEqual(MainOrchestrator(self.root).load(task_id)["status"], "COMPLETED")

    def test_requires_explicit_mock_mode(self):
        task_id = self.prepare("regular")
        with self.assertRaises(SystemExit):
            self.call(self.args + ["orchestration-run", task_id])


if __name__ == "__main__":
    unittest.main()
