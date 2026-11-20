"""Offline checks for the distinct fail-closed production adapter boundary."""
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.production_adapter import ProductionWorkflowAdapter
from alpha_research.orchestration.workflow_contract import (
    command_and_args, project_existing_qualification, validate_workflow_request,
)


class ProductionAdapterContractTests(unittest.TestCase):
    def test_production_execution_cannot_call_gateway_or_provider(self):
        for workflow in ("regular", "super", "optimization"):
            with self.subTest(workflow=workflow), tempfile.TemporaryDirectory() as td:
                adapter = ProductionWorkflowAdapter(td, {})
                runner = MainOrchestrator(td, adapter=adapter)
                request = {"objective": "no live authorization"}
                if workflow == "optimization":
                    request["alpha_id"] = "example-source"
                task = runner.create(workflow, request)
                result = runner.execute(task["task_id"])
                self.assertEqual(result["status"], "BLOCKED")
                self.assertEqual(result["steps"]["validate"], "RUNNING")
                self.assertEqual(result["steps"]["execute"], "PENDING")
                self.assertEqual(result["budget"]["reserved"], 0)
                self.assertEqual(result["budget"]["used"], 0)
                self.assertIsNone(result["recovery_target"])
                with self.assertRaisesRegex(OrchestrationError, "explicit reconciliation"):
                    runner.resume(task["task_id"])

    def test_offline_production_control_plane_reuses_real_dispatch_without_real_gateway(self):
        from alpha_research.orchestration.offline_adapter import MockWorkflowAdapter
        from alpha_research.config import DEFAULT_CONFIG

        class MockGateway:
            is_offline_mock = True

        class MockModel:
            is_offline_mock = True

        for workflow, function, result in (
            ("regular", "run_regular_campaign", {
                "status": "budget_complete", "completed": [
                    {"alpha_ids": ["mock-A"],
                     "candidate_status": {"mock-A": "CANDIDATE"}}]}),
            ("super", "run_super_research", {
                "candidate_id": "mock-S", "candidate_status": "CANDIDATE",
                "preferred_status": "NOT_PREFERRED",
                "candidate_ref": "candidate.json", "preferred_ref": "preferred.json"}),
            ("optimization", "run_optimization", {
                "candidate_id": "mock-O", "candidate_status": "CANDIDATE",
                "preferred_status": "NOT_PREFERRED",
                "candidate_ref": "candidate.json", "preferred_ref": "preferred.json"}),
        ):
            with self.subTest(workflow=workflow), tempfile.TemporaryDirectory() as td:
                agent = type("OfflineAgent", (), {"model": MockModel()})()
                mock = MockWorkflowAdapter(
                    Path(td), DEFAULT_CONFIG, MockGateway(), agent)
                adapter = ProductionWorkflowAdapter(
                    td, DEFAULT_CONFIG, offline_test_adapter=mock)
                runner = MainOrchestrator(td, adapter=adapter)
                request = {"objective": "offline controlled dispatch"}
                if workflow == "optimization":
                    request["alpha_id"] = "synthetic-source"
                task = runner.create(workflow, request)
                with patch("alpha_research.orchestration.dispatch." + function,
                           return_value=result) as domain:
                    completed = runner.execute(task["task_id"])
                self.assertEqual(completed["status"], "COMPLETED")
                self.assertEqual(completed["budget"]["used"], 1)
                self.assertEqual(completed["budget"]["reserved"], 0)
                self.assertEqual(domain.call_count, 1)
                self.assertEqual(runner.resume(task["task_id"]), completed)

    def test_offline_bridge_rejects_custom_dispatcher_and_unmarked_runtime(self):
        from alpha_research.orchestration.offline_adapter import MockWorkflowAdapter

        class FakeGateway:
            is_offline_mock = True

        class FakeModel:
            is_offline_mock = True

        with tempfile.TemporaryDirectory() as td:
            agent = type("OfflineAgent", (), {"model": FakeModel()})()
            custom = MockWorkflowAdapter(
                td, {}, FakeGateway(), agent,
                dispatcher=lambda *a, **kw: {"status": "completed"})
            with self.assertRaisesRegex(OrchestrationError, "offline mock runtime"):
                ProductionWorkflowAdapter(td, {}, offline_test_adapter=custom)
            class BadModel:
                is_offline_mock = False
            agent.model = BadModel()
            with self.assertRaisesRegex(OrchestrationError, "mock model"):
                MockWorkflowAdapter(td, {}, FakeGateway(), agent)

    def test_offline_bridge_unknown_external_outcome_retains_intent_and_budget(self):
        from alpha_research.orchestration.offline_adapter import MockWorkflowAdapter

        class FakeGateway:
            is_offline_mock = True

        class FakeModel:
            is_offline_mock = True

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agent = type("OfflineAgent", (), {"model": FakeModel()})()
            mock = MockWorkflowAdapter(root, {}, FakeGateway(), agent)
            runner = MainOrchestrator(
                td, adapter=ProductionWorkflowAdapter(
                    td, {}, offline_test_adapter=mock))
            task = runner.create("super", {"objective": "offline unknown"})
            target = root / "state" / "research_v1_1" / "super_intent.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"workflow": "super"}), encoding="utf-8")
            def interrupted(*args, **kwargs):
                context = kwargs["execution_context"]
                context["bind_research_identity"]("super:" + "a" * 32)
                context["bind_recovery_target"]("simulation_intent", target)
                raise RuntimeError("synthetic uncertain outcome")
            with patch("alpha_research.orchestration.dispatch.run_super_research",
                       side_effect=interrupted) as domain:
                blocked = runner.execute(task["task_id"])
            self.assertEqual(blocked["status"], "BLOCKED")
            self.assertEqual(blocked["budget"]["reserved"], 1)
            self.assertEqual(blocked["budget"]["used"], 0)
            self.assertEqual(blocked["research_identity"]["research_id"], "super:" + "a" * 32)
            self.assertEqual(blocked["recovery_target"]["ref"], str(target.resolve()))
            with self.assertRaisesRegex(OrchestrationError, "explicit reconciliation"):
                runner.resume(task["task_id"])
            self.assertEqual(domain.call_count, 1)

    def test_pure_contract_cannot_bypass_existing_business_invariants(self):
        with self.assertRaisesRegex(OrchestrationError, "unknown workflow"):
            validate_workflow_request("unknown", {"objective": "example"})
        with self.assertRaisesRegex(OrchestrationError, "Alpha ID"):
            validate_workflow_request("optimization", {"objective": "example"})
        with self.assertRaisesRegex(OrchestrationError, "one-batch"):
            command_and_args(
                "regular", {"objective": "example", "max_batches": 2})
        with self.assertRaisesRegex(OrchestrationError, "one-batch"):
            command_and_args(
                "regular", {"objective": "example", "max_batches": True})
        for workflow, expected in (
                ("regular", "run-campaign"),
                ("super", "run-super"),
                ("optimization", "run-optimization")):
            request = {"objective": "example"}
            if workflow == "optimization":
                request["alpha_id"] = "example-source"
            command, args = command_and_args(workflow, request)
            self.assertEqual(command, expected)
            self.assertEqual(args.max_batches, 1)

    def test_qualification_projection_does_not_requalify_or_synthesize_success(self):
        with self.assertRaisesRegex(OrchestrationError, "missing execution checkpoint"):
            project_existing_qualification("super", {})
        with self.assertRaisesRegex(OrchestrationError, "lacks completed records"):
            project_existing_qualification("regular", {"execute": {"completed": []}})
        with self.assertRaisesRegex(OrchestrationError, "unavailable"):
            project_existing_qualification("super", {
                "execute": {"status": "unresolved"}})
        evidence = {
            "candidate_id": "c",
            "candidate_status": "EVIDENCE_PENDING",
            "preferred_status": "PENDING_PREFERRED",
            "candidate_ref": "candidate.json",
            "preferred_ref": "preferred.json",
        }
        self.assertEqual(
            project_existing_qualification("optimization", {"execute": evidence}),
            evidence)


if __name__ == "__main__":
    unittest.main()
