"""Prepared task -> real domain Dispatch under hardcoded offline Gateway/Model."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from alpha_research.cli import main
from alpha_research.orchestration.engine import MainOrchestrator
from alpha_research.state.records import ResearchRecordStore


class DomainMockCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.prefix = ["--state-root", str(self.root),
                       "--lead", "deepseek", "--executor", "deepseek"]

    def call(self, command):
        out, err = io.StringIO(), io.StringIO()
        with (patch("alpha_research.cli.Gateway",
                    side_effect=AssertionError("real gateway forbidden")),
              patch("alpha_research.cli.build_model_client",
                    side_effect=AssertionError("real model forbidden")),
              redirect_stdout(out), redirect_stderr(err)):
            code = main(self.prefix + command)
        return code, out.getvalue(), err.getvalue()

    def prepare(self, workflow):
        args = ["orchestration-prepare", "--workflow", workflow,
                "--objective", "offline domain integration", "--region", "GLB",
                "--delay", "1"]
        if workflow == "optimization":
            args += ["--alpha-id", "SOURCE"]
        code, out, err = self.call(args)
        self.assertEqual(code, 0, err)
        return json.loads(out)["task_id"]

    def test_super_and_optimization_execute_domain_code_and_persist_intent(self):
        for workflow in ("super", "optimization"):
            with self.subTest(workflow=workflow):
                task_id = self.prepare(workflow)
                code, out, err = self.call([
                    "orchestration-domain-mock", task_id, "--mode", "mock"])
                self.assertEqual(code, 0, err)
                result = json.loads(out)
                self.assertEqual(result["status"], "COMPLETED",
                                 result["execution"].get("failure"))
                self.assertEqual(result["mode"], "offline_domain_mock")
                self.assertEqual(result["mock_simulation_attempts"], 1)
                self.assertFalse(result["model_provider_invoked"])
                self.assertFalse(result["brain_provider_invoked"])
                self.assertNotIn("submit_alpha", result["gateway_calls"])
                task = MainOrchestrator(self.root).load(task_id)
                self.assertEqual(task["budget"]["used"], 1)
                self.assertEqual(task["budget"]["reserved"], 0)
                self.assertIsNotNone(task["research_identity"])
                self.assertEqual(len(ResearchRecordStore(self.root).list("candidate")) >= 1, True)
                self.assertTrue(list(self.root.rglob("*simulation_intent.json")))
                code, _, err = self.call([
                    "orchestration-domain-mock", task_id, "--mode", "mock"])
                self.assertEqual(code, 2)
                self.assertEqual(MainOrchestrator(self.root).load(task_id)["budget"]["used"], 1)

    def test_lost_response_blocks_without_automatic_replay(self):
        task_id = self.prepare("super")
        code, out, err = self.call([
            "orchestration-domain-mock", task_id, "--mode", "mock",
            "--fault", "lost-simulation-response"])
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["mock_simulation_attempts"], 1)
        task = MainOrchestrator(self.root).load(task_id)
        self.assertEqual(task["budget"]["reserved"], 1)
        self.assertEqual(task["budget"]["used"], 0)
        self.assertIsNotNone(task["recovery_target"])
        code, _, err = self.call([
            "orchestration-domain-mock", task_id, "--mode", "mock"])
        self.assertEqual(code, 2)
        self.assertEqual(MainOrchestrator(self.root).load(task_id)["budget"]["reserved"], 1)

    def test_regular_cannot_enter_super_only_fixture(self):
        task_id = self.prepare("regular")
        code, _, err = self.call([
            "orchestration-domain-mock", task_id, "--mode", "mock"])
        self.assertEqual(code, 2)
        self.assertEqual(MainOrchestrator(self.root).load(task_id)["status"], "CREATED")


if __name__ == "__main__":
    unittest.main()
