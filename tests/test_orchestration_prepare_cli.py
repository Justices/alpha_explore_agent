"""CLI-only offline task preparation and read-only handoff inspection."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from alpha_research.cli import main
from alpha_research.orchestration.engine import MainOrchestrator


class OfflineProductionPreparationCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "scope": {"region": "CHN", "delay": 1},
            "model_governance": {
                "max_calls": 4, "max_reported_tokens": 1000,
                "failover": "manual",
            },
        }), encoding="utf-8")
        self.prefix = [
            "--state-root", str(self.root), "--config", str(self.config),
            "--lead", "deepseek", "--executor", "deepseek",
        ]

    def _call(self, args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with (patch("alpha_research.cli.Gateway",
                    side_effect=AssertionError("gateway unexpectedly created")),
              patch("alpha_research.cli.build_model_client",
                    side_effect=AssertionError("model unexpectedly created")),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            status = main(args)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_regular_super_optimization_prepare_without_external_calls(self):
        for workflow in ("regular", "super", "optimization"):
            with self.subTest(workflow=workflow):
                args = self.prefix + [
                    "orchestration-prepare", "--workflow", workflow,
                    "--objective", "offline preparation",
                ]
                if workflow == "optimization":
                    args += ["--alpha-id", "offline-source"]
                status, output, error = self._call(args)
                self.assertEqual(status, 0, error)
                result = json.loads(output)
                self.assertEqual(result["status"], "PREPARED_NOT_AUTHORIZED")
                self.assertFalse(result["model_call_authorized"])
                task_id = result["task_id"]
                task = MainOrchestrator(self.root).load(task_id)
                self.assertEqual(task["status"], "CREATED")
                self.assertEqual(task["request"]["region"], "CHN")
                self.assertEqual(task["request"]["delay"], 1)
                self.assertEqual(task["budget"]["reserved"], 0)
                status, output, error = self._call(
                    self.prefix + ["orchestration-handoff", task_id])
                self.assertEqual(status, 0, error)
                inspected = json.loads(output)
                self.assertEqual(inspected["status"], "AWAITING_DOMAIN_RESEARCH_ID")
                self.assertFalse(inspected["inspection_provider_invoked"])
                self.assertFalse(inspected["inspection_brain_invoked"])

    def test_prepare_requires_both_registered_roles_and_policy(self):
        missing_role = [
            "--state-root", str(self.root), "--config", str(self.config),
            "--lead", "deepseek", "orchestration-prepare",
            "--workflow", "regular", "--objective", "offline",
        ]
        status, _, _ = self._call(missing_role)
        self.assertEqual(status, 2)
        self.assertFalse((self.root / "state" / "research_v1_1" /
                          "orchestration").exists())

    def test_scope_conflicts_and_missing_region_fail_before_creating_task(self):
        inconsistent = self.prefix + [
            "orchestration-prepare", "--workflow", "regular",
            "--objective", "offline", "--region", "USA",
        ]
        status, _, _ = self._call(inconsistent)
        self.assertEqual(status, 2)

        bad_delay = self.prefix + [
            "orchestration-prepare", "--workflow", "regular",
            "--objective", "offline", "--delay", "0",
        ]
        status, _, _ = self._call(bad_delay)
        self.assertEqual(status, 2)

        missing_scope = json.loads(self.config.read_text(encoding="utf-8"))
        missing_scope["scope"]["region"] = None
        self.config.write_text(json.dumps(missing_scope), encoding="utf-8")
        status, _, _ = self._call(
            self.prefix + ["orchestration-prepare", "--workflow", "super",
                           "--objective", "offline"])
        self.assertEqual(status, 2)
        self.assertFalse((self.root / "state" / "research_v1_1" /
                          "orchestration").exists())

    def test_inspection_rejects_model_policy_drift(self):
        status, output, error = self._call(
            self.prefix + ["orchestration-prepare", "--workflow", "regular",
                           "--objective", "offline"])
        self.assertEqual(status, 0, error)
        task_id = json.loads(output)["task_id"]
        new_config = json.loads(self.config.read_text(encoding="utf-8"))
        new_config["model_governance"]["max_calls"] = 5
        self.config.write_text(json.dumps(new_config), encoding="utf-8")
        status, _, error = self._call(
            self.prefix + ["orchestration-handoff", task_id])
        self.assertEqual(status, 2)
        self.assertIn("OrchestrationError", error)


if __name__ == "__main__":
    unittest.main()
