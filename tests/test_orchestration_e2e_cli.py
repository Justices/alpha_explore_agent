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
from alpha_research.orchestration.execution_trace import inspect_execution_trace


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
        self.assertEqual(report["trace_status"], "TRACE_VERIFIED")
        self.assertEqual(report["workflow_handoff_audit"]["status"], "VERIFIED")
        self.assertTrue(report["trace_binding_valid"])
        self.assertGreater(report["trace_event_count"], 0)
        self.assertEqual(len(report["trace_last_event_sha256"]), 64)
        self.assertTrue(Path(report["trace_ref"]).is_file())
        self.assertGreater(report["evidence_file_count"], 0)
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
            "alpha_ids"], ["OFFLINE_REGULAR_ALPHA_0001"])
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
                self.assertEqual(result["trace_status"], "TRACE_VERIFIED")
                self.assertEqual(result["workflow_handoff_audit"]["status"], "VERIFIED")
                self.assertTrue(Path(result["trace_ref"]).is_file())
                self.assertIn("simulate_super", result["scripted_gateway_commands"])
                self.assertFalse(result["brain_simulation_invoked"])
                self.assertFalse(result["provider_model_invoked"])
                task = MainOrchestrator(root).load(result["task_id"])
                self.assertEqual(task["status"], "COMPLETED")
                self.assertEqual(task["budget"]["used"], 1)
                self.assertEqual(task["steps"]["collect"], "DONE")
                self.assertIsNotNone(task["recovery_target"])
                trace = inspect_execution_trace(root, result["task_id"])
                self.assertEqual(trace["status"], "TRACE_VERIFIED")
                event_types = [event["event_type"] for event in trace["events"]]
                if workflow == "super":
                    self.assertIn("super.proposal_generated", event_types)
                    self.assertIn("super.simulation_readback_completed", event_types)
                    self.assertIn("super.qualification_completed", event_types)
                    self.assertIsNotNone(trace["domain_evidence"]["super_run"])
                else:
                    self.assertIn("optimization.source_snapshot_captured", event_types)
                    self.assertIn("optimization.proposal_generated", event_types)
                    self.assertIn("optimization.simulation_readback_completed", event_types)
                    self.assertIn("optimization.qualification_completed", event_types)
                    self.assertIsNotNone(trace["domain_evidence"]["optimization"]["branch"])

    def test_successful_domain_does_not_pass_with_missing_trace_evidence(self):
        with patch(
            "alpha_research.orchestration.execution_trace.inspect_execution_trace",
            return_value={
                "status": "TRACE_INCOMPLETE",
                "trace_binding_valid": False,
                "journal_verification": {
                    "journal_ref": "missing", "event_count": 0,
                    "last_event_sha256": None,
                },
                "evidence_manifest": {"status": "PARTIAL", "file_count": 0},
            },
        ):
            code, output, error = self.invoke(extra=["--dataset", "analyst10"])
        self.assertEqual(code, 2, error)
        report = json.loads(output)
        self.assertEqual(report["task_status"], "COMPLETED")
        self.assertEqual(report["status"], "OFFLINE_E2E_BLOCKED")
        self.assertEqual(report["trace_status"], "TRACE_INCOMPLETE")
        self.assertFalse(report["trace_binding_valid"])
        self.assertFalse(report["production_authorized"])

    def test_trace_with_missing_domain_handoff_cannot_pass_e2e(self):
        from alpha_research.orchestration.execution_trace import inspect_execution_trace
        real_inspector = inspect_execution_trace

        def incomplete_trace(root, task_id):
            result = real_inspector(root, task_id)
            result["events"] = [
                row for row in result["events"]
                if row["event_type"] != "regular.campaign_completed"
            ]
            return result

        with patch(
            "alpha_research.orchestration.execution_trace.inspect_execution_trace",
            side_effect=incomplete_trace,
        ):
            code, output, error = self.invoke(extra=["--dataset", "analyst10"])
        self.assertEqual(code, 2, error)
        report = json.loads(output)
        self.assertEqual(report["task_status"], "COMPLETED")
        self.assertEqual(report["trace_status"], "TRACE_VERIFIED")
        self.assertEqual(report["workflow_handoff_audit"]["status"], "INCOMPLETE")
        self.assertIn("regular.campaign_completed",
                      report["workflow_handoff_audit"]["missing_events"])
        self.assertFalse(report["production_authorized"])

    def test_trace_with_mismatched_domain_identity_cannot_pass_e2e(self):
        from alpha_research.orchestration.execution_trace import inspect_execution_trace
        real_inspector = inspect_execution_trace

        def mismatched_trace(root, task_id):
            result = real_inspector(root, task_id)
            for row in result["events"]:
                if row["event_type"] == "super.qualification_completed":
                    row["payload"] = {**row["payload"], "candidate_id": "forged"}
            return result

        with patch(
            "alpha_research.orchestration.execution_trace.inspect_execution_trace",
            side_effect=mismatched_trace,
        ):
            code, output, error = self.invoke("super")
        self.assertEqual(code, 2, error)
        report = json.loads(output)
        self.assertEqual(report["task_status"], "COMPLETED")
        self.assertEqual(report["workflow_handoff_audit"]["status"], "INCOMPLETE")
        self.assertFalse(report["workflow_handoff_audit"]["domain_evidence_bound"])
        self.assertFalse(report["production_authorized"])

    def test_lost_scripted_result_blocks_and_refuses_replay(self):
        code, output, error = self.invoke(extra=[
            "--dataset", "analyst10", "--fault", "lost-simulation-response",
        ])
        self.assertEqual(code, 2, error)
        report = json.loads(output)
        self.assertEqual(report["status"], "OFFLINE_E2E_BLOCKED")
        self.assertEqual(report["trace_status"], "TRACE_VERIFIED")
        self.assertEqual(report["scripted_domain_attempts"], 1)
        self.assertFalse(report["automatic_retry_allowed"])
        task = MainOrchestrator(self.root).load(report["task_id"])
        self.assertEqual(task["status"], "BLOCKED")
        self.assertEqual(task["budget"], {
            "limit": 1, "used": 0, "reserved": 1})
        self.assertIsNotNone(task["recovery_target"])
        batch = Path(task["recovery_target"]["ref"])
        self.assertEqual(
            json.loads((batch / "simulation_intent.json").read_text())["status"],
            "OUTCOME_UNKNOWN",
        )
        self.assertFalse((batch / "results.json").exists())
        trace = inspect_execution_trace(self.root, report["task_id"])
        self.assertEqual(trace["status"], "TRACE_VERIFIED")
        self.assertIn("task.step_blocked", [row["event_type"] for row in trace["events"]])
        self.assertTrue(trace["evidence_manifest"]["files"])
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

    def test_reject_missing_roles_but_accept_bounded_multi_dataset_campaign(self):
        code, _, error = self.invoke(executor="")
        self.assertEqual(code, 2)
        self.assertIn("Lead and Executor", error)
        self.assertFalse(marker_path(self.root).exists())
        code, output, error = self.invoke(extra=[
            "--dataset", "analyst10", "--dataset", "analyst11",
            "--category", "OTHER",
        ])
        self.assertEqual(code, 0, error)
        report = json.loads(output)
        self.assertEqual(report["status"], "OFFLINE_E2E_PASSED")
        self.assertEqual(report["expected_domain_attempts"], 2)
        self.assertEqual(report["scripted_domain_attempts"], 2)
        self.assertEqual(report["completed_dataset_batches"], 2)
        self.assertEqual(report["trace_status"], "TRACE_VERIFIED")
        self.assertEqual(report["workflow_handoff_audit"]["status"], "VERIFIED")
        task = MainOrchestrator(self.root).load(report["task_id"])
        self.assertEqual(task["request"]["dataset_ids"], ["analyst10", "analyst11"])
        self.assertEqual(
            {row["dataset_id"] for row in task["checkpoints"]["execute"]["completed"]},
            {"analyst10", "analyst11"},
        )
        self.assertTrue(marker_path(self.root).is_file())

    def test_external_live_config_is_not_rewritten_by_offline_command(self):
        custom = self.base / "scope.json"
        original = json.dumps({
            "scope": {"region": "CHN", "delay": 1,
                      "universe": "TOP3000", "dataset_id": "analyst10"},
            "model_governance": {"max_calls": -999},
        })
        custom.write_text(original, encoding="utf-8")
        # Never migrate another root's config merely to run offline fixtures.
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
        self.assertIn("only its State Root default config", err.getvalue())
        self.assertEqual(custom.read_text(encoding="utf-8"), original)
        self.assertFalse(marker_path(self.root).exists())


if __name__ == "__main__":
    unittest.main()
