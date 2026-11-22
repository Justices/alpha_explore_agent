"""P4.20 unified read-only control-plane view, no user JSON or provider."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from alpha_research.cli import main
from alpha_research.orchestration.engine import MainOrchestrator
from alpha_research.orchestration.research_control_plane import (
    inspect_research_control_plane,
)
from alpha_research.orchestration.mock_workspace import claim_mock_workspace
from alpha_research.orchestration.research_preparation import prepare_offline_research
from alpha_research.orchestration.runtime_config import (
    prepare_registered_model_runtime,
)
from alpha_research.agents.registry import bundled_registry_path
from alpha_research.orchestration.execution_identity import persist_execution_identity
from alpha_research.orchestration.execution_proposal import persist_execution_proposal


class ResearchControlPlaneTests(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.runner = MainOrchestrator(self.root)
        config = prepare_registered_model_runtime(
            {"model_governance": {
                "max_calls": 1, "max_reported_tokens": 4096, "failover": "manual"}},
            {"research_lead": "deepseek", "research_executor": "deepseek"},
            bundled_registry_path(),
        )
        self.model = config["model"]
        self.policy = config["model_governance"]

    def fresh(self, workflow="super"):
        request = {
            "objective": "single-view offline research progress",
            "region": "USA", "delay": 1, "universe": "TOP3000",
        }
        if workflow == "optimization":
            request["alpha_id"] = "SOURCE"
        task = self.runner.create(workflow, request)
        persist_execution_proposal(self.runner, task["task_id"])
        persist_execution_identity(
            self.runner, task["task_id"], self.model, self.policy)
        return task["task_id"]

    def summary(self, task_id):
        return inspect_research_control_plane(
            self.runner, task_id, self.model, self.policy)

    def test_fresh_unstarted_task_has_explicit_research_blocker(self):
        task_id = self.fresh()
        before = sorted(str(p) for p in self.root.rglob("*"))
        report = self.summary(task_id)
        self.assertEqual(report["status"], "RESEARCH_OUTPUT_NOT_READY_LIVE_BLOCKED")
        self.assertEqual(report["task_status"], "CREATED")
        self.assertEqual(report["components"]["model_attempt"], "NO_MODEL_ATTEMPT")
        self.assertEqual(report["components"]["manifest"], "AWAITING_RESEARCH_OUTPUT")
        self.assertEqual(report["components"]["grant_audit"], "NOT_RESERVED")
        self.assertEqual(report["components"]["live_plan"], "RESEARCH_RUNTIME_LIVE_BLOCKED")
        self.assertIn("RESEARCH_EXTERNAL_GRANT_CONSUMER_UNAVAILABLE",
                      report["live_blockers"])
        self.assertEqual(report["reconciliation_components"], [])
        self.assertEqual(report["simulation_budget"],
                         {"limit": 1, "used": 0, "reserved": 0})
        for key in ("manual_manifest_required", "model_call_authorized",
                    "brain_read_authorized", "brain_simulation_authorized",
                    "alpha_submission_authorized", "permission_to_dispatch"):
            self.assertFalse(report[key])
        self.assertNotIn("single-view offline research", json.dumps(report))
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

    def test_integrated_offline_workflow_updates_status_without_consuming_simulation(self):
        task_id = self.fresh("optimization")
        claim_mock_workspace(self.root)
        before = self.summary(task_id)
        self.assertEqual(before["status"], "RESEARCH_OUTPUT_NOT_READY_LIVE_BLOCKED")
        completed = prepare_offline_research(self.runner, task_id)
        self.assertEqual(completed["status"], "RESEARCH_PREPARED_OFFLINE")
        paths_before = sorted(str(p) for p in self.root.rglob("*"))
        report = self.summary(task_id)
        self.assertEqual(
            report["status"], "AWAITING_INDEPENDENT_RESEARCH_AUTHORIZATION")
        self.assertEqual(
            report["components"]["research_preparation"], "RESEARCH_PREPARED_OFFLINE")
        self.assertEqual(
            report["components"]["model_attempt"], "MODEL_MOCK_COMPLETED")
        self.assertEqual(
            report["components"]["manifest"], "GENERATED_LOCAL_NOT_AUTHORIZED")
        self.assertEqual(report["next_required"],
                         "INDEPENDENT_RESEARCH_ISSUER_AND_GLOBAL_CONSUMER")
        self.assertFalse(report["model_call_authorized"])
        self.assertFalse(report["permission_to_dispatch"])
        self.assertEqual(report["simulation_budget"]["used"], 0)
        self.assertEqual(paths_before, sorted(str(p) for p in self.root.rglob("*")))

    def test_damaged_terminal_journal_requires_reconciliation(self):
        task_id = self.fresh()
        claim_mock_workspace(self.root)
        prepare_offline_research(self.runner, task_id)
        path = (self.root / "state" / "research_v1_1" /
                "research_preparation" / (task_id + ".json"))
        row = json.loads(path.read_text(encoding="utf-8"))
        row["manifest_sha256"] = "f" * 64
        path.write_text(json.dumps(row), encoding="utf-8")
        before = sorted(str(p) for p in self.root.rglob("*"))
        result = self.summary(task_id)
        self.assertEqual(result["status"], "RECONCILIATION_REQUIRED")
        self.assertIn("research_preparation", result["reconciliation_components"])
        self.assertEqual(
            result["components"]["research_preparation"], "RECONCILIATION_REQUIRED")
        self.assertFalse(result["permission_to_dispatch"])
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

    def test_pending_model_attempt_forces_reconciliation(self):
        from alpha_research.orchestration.research_runtime_adapter import (
            OfflineResearchRuntimeAdapter,
        )
        from alpha_research.orchestration.domain_mock import (
            DomainMockGateway, DomainMockModel,
        )
        task_id = self.fresh()
        claim_mock_workspace(self.root)
        adapter = OfflineResearchRuntimeAdapter(
            self.runner, task_id, DomainMockGateway("USA"), DomainMockModel(),
            self.policy)
        with adapter:
            adapter.reserve_scripted_model_call(
                "construct_super", {"region": "USA"})
        summary = self.summary(task_id)
        self.assertEqual(summary["status"], "RECONCILIATION_REQUIRED")
        self.assertEqual(summary["components"]["model_attempt"],
                         "MODEL_CALL_MAY_HAVE_STARTED")
        self.assertIn("model_attempt", summary["reconciliation_components"])
        self.assertFalse(summary["automatic_retry_allowed"])
        self.assertEqual(self.runner.load(task_id)["budget"]["used"], 0)

    def test_manifest_without_verified_model_chain_is_not_authorization_ready(self):
        task_id = self.fresh()
        # A separately pinned artifact can exist without a completed Agent
        # journal; the aggregate must not mistake it for trusted readiness.
        stub = {
            "schema_version": 1, "task_id": task_id,
            "status": "GENERATED_LOCAL_NOT_AUTHORIZED",
            "permission_to_dispatch": False,
        }
        with patch(
            "alpha_research.orchestration.research_control_plane.inspect_generated_manifest",
            return_value=stub,
        ):
            report = self.summary(task_id)
        self.assertEqual(report["status"],
                         "RESEARCH_OUTPUT_NOT_READY_LIVE_BLOCKED")
        self.assertEqual(report["next_required"],
                         "VERIFY_RESEARCH_PREPARATION_CHAIN")
        self.assertFalse(report["permission_to_dispatch"])

    def test_orphan_completed_model_attempt_requires_reconciliation(self):
        task_id = self.fresh()
        stub = {
            "schema_version": 1, "task_id": task_id,
            "status": "MODEL_MOCK_COMPLETED",
            "permission_to_dispatch": False,
        }
        with patch(
            "alpha_research.orchestration.research_control_plane.inspect_runtime_attempt",
            return_value=stub,
        ):
            report = self.summary(task_id)
        self.assertEqual(report["status"], "RECONCILIATION_REQUIRED")
        self.assertIn("model_attempt", report["reconciliation_components"])

    def test_terminal_preparation_without_model_and_manifest_fails_closed(self):
        task_id = self.fresh()
        stub = {
            "schema_version": 1, "task_id": task_id,
            "status": "RESEARCH_PREPARED_OFFLINE",
            "permission_to_dispatch": False,
        }
        with patch(
            "alpha_research.orchestration.research_control_plane.inspect_research_preparation",
            return_value=stub,
        ):
            report = self.summary(task_id)
        self.assertEqual(report["status"], "RECONCILIATION_REQUIRED")
        self.assertIn("research_preparation", report["reconciliation_components"])

    def test_unrecognized_or_cross_task_component_verdict_fails_closed(self):
        task_id = self.fresh()
        for status, claimed_task_id in (
            ("UNEXPECTED_READY", task_id),
            ("NOT_RESERVED", "0" * 32),
            ("NOT_RESERVED", task_id),
        ):
            with self.subTest(status=status, task_id=claimed_task_id):
                fake = {
                    "schema_version": True if claimed_task_id == task_id
                    and status == "NOT_RESERVED" else 1,
                    "task_id": claimed_task_id,
                    "status": status,
                    "permission_to_dispatch": False,
                }
                with patch(
                    "alpha_research.orchestration.research_control_plane.inspect_research_grant_claim",
                    return_value=fake,
                ):
                    report = self.summary(task_id)
                self.assertEqual(report["status"], "RECONCILIATION_REQUIRED")
                self.assertIn("grant_audit", report["reconciliation_components"])

    def test_live_plan_must_prove_negative_permission_and_scope(self):
        task_id = self.fresh()
        for claimed_task_id, permission in ((task_id, True), ("f" * 32, False)):
            with self.subTest(claimed_task_id=claimed_task_id, permission=permission):
                fake = {
                    "schema_version": 1, "task_id": claimed_task_id,
                    "status": "RESEARCH_RUNTIME_LIVE_BLOCKED",
                    "blockers": ["EXTERNAL_APPROVAL_REQUIRED"],
                    "permission_to_dispatch": permission,
                    "live_research_authorized": False,
                }
                with patch(
                    "alpha_research.orchestration.research_control_plane.inspect_research_runtime_plan",
                    return_value=fake,
                ):
                    report = self.summary(task_id)
                self.assertEqual(report["status"], "RECONCILIATION_REQUIRED")
                self.assertIn("live_plan", report["reconciliation_components"])
                self.assertFalse(report["permission_to_dispatch"])

    def test_cli_one_command_never_builds_gateway_or_model(self):
        prefix = [
            "--state-root", str(self.root),
            "--lead", "deepseek", "--executor", "deepseek",
        ]
        out, err = io.StringIO(), io.StringIO()
        with (patch("alpha_research.cli.Gateway",
                    side_effect=AssertionError("BRAIN connection forbidden")),
              patch("alpha_research.cli.build_model_client",
                    side_effect=AssertionError("paid model forbidden")),
              redirect_stdout(out), redirect_stderr(err)):
            result = main(prefix + [
                "orchestration-prepare", "--workflow", "super",
                "--objective", "read-only summary CLI",
                "--region", "USA", "--delay", "1", "--universe", "TOP3000"])
        self.assertEqual(result, 0, err.getvalue())
        task_id = json.loads(out.getvalue())["task_id"]
        before = sorted(str(p) for p in self.root.rglob("*"))
        out, err = io.StringIO(), io.StringIO()
        with (patch("alpha_research.cli.Gateway",
                    side_effect=AssertionError("BRAIN connection forbidden")),
              patch("alpha_research.cli.build_model_client",
                    side_effect=AssertionError("paid model forbidden")),
              redirect_stdout(out), redirect_stderr(err)):
            result = main(prefix + ["orchestration-research-status", task_id])
        self.assertEqual(result, 0, err.getvalue())
        report = json.loads(out.getvalue())
        self.assertEqual(report["status"], "RESEARCH_OUTPUT_NOT_READY_LIVE_BLOCKED")
        self.assertFalse(report["permission_to_dispatch"])
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))


if __name__ == "__main__":
    unittest.main()
