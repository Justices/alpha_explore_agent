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
        self.assertEqual(
            result["components"]["research_preparation"], "RECONCILIATION_REQUIRED")
        self.assertFalse(result["permission_to_dispatch"])
        self.assertEqual(before, sorted(str(p) for p in self.root.rglob("*")))

    def test_cli_one_command_never_builds_gateway_or_model(self):
        from contextlib import ExitStack
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
