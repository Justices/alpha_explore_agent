"""Offline checks for the distinct fail-closed production adapter boundary."""
import tempfile
import unittest

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

    def test_pure_contract_cannot_bypass_existing_business_invariants(self):
        with self.assertRaisesRegex(OrchestrationError, "unknown workflow"):
            validate_workflow_request("unknown", {"objective": "example"})
        with self.assertRaisesRegex(OrchestrationError, "Alpha ID"):
            validate_workflow_request("optimization", {"objective": "example"})
        with self.assertRaisesRegex(OrchestrationError, "one-batch"):
            command_and_args(
                "regular", {"objective": "example", "max_batches": 2})
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
        with self.assertRaisesRegex(OrchestrationError, "no completed batch"):
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
