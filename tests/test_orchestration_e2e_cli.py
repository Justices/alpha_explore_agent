"""One-command offline Task->Campaign/Domain acceptance and safety tests.

Every test forbids the real BRAIN Gateway and real model factory. Offline
simulation/batch artifacts are synthetic, never authorized submissions.
"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from alpha_research.cli import main
from alpha_research.orchestration.engine import MainOrchestrator
from alpha_research.orchestration.mock_workspace import marker_path


class OfflineEndToEndCliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.root = self.base / "fresh-offline"

    def invoke(self, workflow="regular", *, extra=None, root=None,
               lead="deepseek", executor="deepseek"):
        root = self.root if root is None else root
        args = [
            "--state-root", str(root),
            "--lead", lead, "--executor", executor,
            "orchestration-e2e-offline", "--mode", "mock",
            "--workflow", workflow, "--objective", "one full offline task",
            "--region", "USA", "--delay", "1", "--universe", "TOP3000",
        ] + (extra or [])
        out, err = io.StringIO(), io.StringIO()
        with (patch("alpha_research.cli.Gateway",
                    side_effect=AssertionError("real Gateway forbidden")),
              patch("alpha_research.cli.build_model_client",
                    side_effect=AssertionError("real Model forbidden")),
              redirect_stdout(out), redirect_stderr(err)):
            rc = main(args)
        return rc, out.getvalue(), err.getvalue()

    def test_regular_pinned_analyst10_all_four_checkpoints_and_no_network(self):
        code, output, error = self.invoke(extra=[
            "--dataset", "analyst10", "--category", "OTHER",
        ])
        self.assertEqual(code, 0, error)
        report = json.loads(output)
        self.assertEqual(report["status"], "OFFLINE_E2E_PASSED")
        self.assertEqual(report["workflow"], "regular")
        self.assertTrue(report["research_identity_bound"])
        self.assertTrue(report["recovery_target_bound"])
        self.assertEqual(report["scripted_domain_attempts"], 1)
        self.assertIn("get_datasets", report["scripted_gateway_commands"])
        self.assertEqual(report["steps"], {
            "validate": "DONE", "execute": "DONE",
            "qualify": "DONE", "collect": "DONE"})
        self.assertEqual(report["budget"], {
            "limit": 1, "used": 1, "reserved": 0})
        for key in ("provider_model_invoked", "brain_invoked",
                    "brain_simulation_invoked", "alpha_submission_invoked",
                    "production_authorized", "automatic_retry_allowed"):
            self.assertFalse(report[key], key)
        task = MainOrchestrator(self.root).load(report["task_id"])
        self.assertEqual(task["request"]["dataset_ids"], ["analyst10"])
        self.assertEqual(task["request"]["category"], "OTHER")
        self.assertEqual(task["checkpoints"]["execute"]["batches_completed"], 1)
        self.assertEqual(task["result"]["qualification"]["completed"][0][
            "alpha_ids"], ["OFFLINE_REGULAR_ALPHA"])
        self.assertEqual(json.loads(Path(report["report_path"]).read_text(
            encoding="utf-8"))["status"], "OFFLINE_E2E_PASSED")
        self.assertTrue(marker_path(self.root).is_file())

    def test_super_and_optimization_use_real_domain_dispatch_with_mocks(self):
        for workflow in ("super", "optimization"):
            with self.subTest(workflow=workflow):
                root = self.base / workflow
                options = ["--alpha-id", "SOURCE"] if workflow == "optimization" else []
                code, output, error = self.invoke(workflow, root=root, extra=options)
                self.assertEqual(code, 0, error)
                result = json.loads(output)
                self.assertEqual(result["status"], "OFFLINE_E2E_PASSED")
                self.assertEqual(result["scripted_domain_attempts"], 1)
                self.assertIn("simulate_super", result["scripted_gateway_commands"])
                self.assertFalse(result["brain_simulation_invoked"])
                self.assertFalse(result["provider_model_invoked"])
                task = MainOrchestrator(root).load(result["task_id"])
                self.assertEqual(task["status"], "COMPLETED")
                self.assertEqual(task["budget"]["used"], 1)
                self.assertEqual(task["steps"]["collect"], "DONE")
                self.assertIsNotNone(task["recovery_target"])

    def test_lost_scripted_result_blocks_and_refuses_replay(self):
        code, output, error = self.invoke(extra=[
            "--dataset", "analyst10", "--fault", "lost-simulation-response",
        ])
        self.assertEqual(code, 2, error)
        report = json.loads(output)
        self.assertEqual(report["status"], "OFFLINE_E2E_BLOCKED")
        self.assertEqual(report["scripted_domain_attempts"], 1)
        self.assertFalse(report["automatic_retry_allowed"])
        task = MainOrchestrator(self.root).load(report["task_id"])
        self.assertEqual(task["status"], "BLOCKED")
        self.assertEqual(task["budget"], {
            "limit": 1, "used": 0, "reserved": 1})
        self.assertIsNotNone(task["recovery_target"])
        self.assertEqual(json.loads(Path(report["report_path"]).read_text(
            encoding="utf-8"))["status"], "OFFLINE_E2E_BLOCKED")
        second, out, err = self.invoke(extra=["--dataset", "analyst10"])
        self.assertEqual(second, 2)
        self.assertEqual(out, "")
        self.assertIn("fresh empty", err)

    def test_refuse_existing_research_workspace_without_touching_it(self):
        self.root.mkdir()
        sentinel = self.root / "existing-research.txt"
        sentinel.write_text("important", encoding="utf-8")
        code, output, error = self.invoke()
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("fresh empty", error)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "important")
        self.assertFalse(marker_path(self.root).exists())

    def test_reject_missing_roles_and_multi_dataset_before_workspace_claim(self):
        code, _, error = self.invoke(executor="")
        self.assertEqual(code, 2)
        self.assertIn("Lead and Executor", error)
        self.assertFalse(marker_path(self.root).exists())
        code, _, error = self.invoke(extra=[
            "--dataset", "analyst10", "--dataset", "analyst11",
        ])
        self.assertEqual(code, 2)
        self.assertIn("at most one", error)
        self.assertFalse(marker_path(self.root).exists())

    def test_explicit_scope_config_conflicts_are_not_silently_bypassed(self):
        custom = self.base / "scope.json"
        custom.write_text(json.dumps({
            "scope": {"region": "CHN", "delay": 1,
                      "universe": "TOP3000", "dataset_id": "analyst10"},
            "model_governance": {"max_calls": -999},
        }), encoding="utf-8")
        # Externally supplied config is independent of the empty mock root.
        args = [
            "--state-root", str(self.root), "--config", str(custom),
            "--lead", "deepseek", "--executor", "deepseek",
            "orchestration-e2e-offline", "--mode", "mock",
            "--workflow", "regular", "--objective", "conflicting scope",
            "--region", "USA", "--dataset", "analyst10",
        ]
        out, err = io.StringIO(), io.StringIO()
        with (patch("alpha_research.cli.Gateway",
                    side_effect=AssertionError("real Gateway forbidden")),
              redirect_stdout(out), redirect_stderr(err)):
            rc = main(args)
        self.assertEqual(rc, 2)
        self.assertIn("conflicts with configured scope", err.getvalue())
        self.assertFalse(marker_path(self.root).exists())


if __name__ == "__main__":
    unittest.main()
