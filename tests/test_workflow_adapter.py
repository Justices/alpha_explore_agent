"""Mock-only integration contract for existing V1.1 workflow dispatch."""
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from alpha_research.config import DEFAULT_CONFIG
from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.offline_adapter import MockWorkflowAdapter


class FakeGateway:
    is_offline_mock = True


class UnsafeGateway:
    pass


class WorkflowAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls = []

    def _adapter(self, output):
        def fake_dispatch(command, args, cfg, root, gateway, agent):
            self.calls.append((command, args.objective, args.alpha_id,
                               args.max_batches))
            return output
        return MockWorkflowAdapter(self.root, DEFAULT_CONFIG, FakeGateway(),
                                   object(), dispatcher=fake_dispatch)

    def test_actual_dispatch_routes_existing_executor_functions(self):
        # Exercise real entrypoints.dispatch, not a replacement dispatcher.
        outputs = {
            "regular": {"status": "budget_complete", "completed": [
                {"alpha_ids": ["A1"], "candidate_status": {"A1": "EVIDENCE_PENDING"}}]},
            "super": {"candidate_id": "C1", "candidate_status": "EVIDENCE_PENDING",
                      "preferred_status": "PENDING_PREFERRED",
                      "candidate_ref": "c.json", "preferred_ref": "p.json"},
            "optimization": {"candidate_id": "C2", "candidate_status": "EVIDENCE_PENDING",
                             "preferred_status": "PENDING_PREFERRED",
                             "candidate_ref": "c2.json", "preferred_ref": "p2.json"},
        }
        functions = {
            "regular": "run_regular_campaign",
            "super": "run_super_research",
            "optimization": "run_optimization",
        }
        for workflow in outputs:
            with self.subTest(workflow=workflow):
                agent = type("Agent", (), {"model": type("Model", (), {"is_offline_mock": True})()})()
                adapter = MockWorkflowAdapter(self.root, DEFAULT_CONFIG,
                                              FakeGateway(), agent)
                request = {"objective": "offline"}
                if workflow == "optimization":
                    request["alpha_id"] = "SOURCE"
                with patch("alpha_research.orchestration.dispatch." + functions[workflow],
                           return_value=outputs[workflow]) as mocked:
                    result = adapter.run(workflow, "execute", request, {})
                    self.assertEqual(result, outputs[workflow])
                    mocked.assert_called_once()
                    if workflow == "regular":
                        self.assertEqual(mocked.call_args.kwargs["max_batches"], 1)
                    if workflow == "optimization":
                        self.assertEqual(mocked.call_args.kwargs["alpha_id"], "SOURCE")

    def test_real_dispatch_rejects_unmarked_model(self):
        agent = type("Agent", (), {"model": object()})()
        with self.assertRaisesRegex(OrchestrationError, "mock model"):
            MockWorkflowAdapter(self.root, DEFAULT_CONFIG, FakeGateway(), agent)

    def test_rejects_gateway_without_offline_marker(self):
        with self.assertRaises(OrchestrationError):
            MockWorkflowAdapter(self.root, DEFAULT_CONFIG, UnsafeGateway(), object())

    def test_three_workflow_routing_and_no_second_qualification(self):
        cases = (
            ("regular", {"status": "budget_complete", "completed": [
                {"alpha_ids": ["A1"], "candidate_status": {"A1": "CANDIDATE"}}]},
             "run-campaign"),
            ("super", {"candidate_id": "c1", "candidate_status": "CANDIDATE",
                       "preferred_status": "PREFERRED", "candidate_ref": "candidate.json",
                       "preferred_ref": "preferred.json"}, "run-super"),
            ("optimization", {"candidate_id": "c2", "candidate_status": "EVIDENCE_PENDING",
                              "preferred_status": "PENDING_PREFERRED",
                              "candidate_ref": "candidate2.json",
                              "preferred_ref": "preferred2.json"}, "run-optimization"),
        )
        for workflow, output, expected_command in cases:
            with self.subTest(workflow=workflow):
                self.calls.clear()
                adapter = self._adapter(output)
                runner = MainOrchestrator(self.root, adapter=adapter)
                request = {"objective": "offline"}
                if workflow == "optimization":
                    request["alpha_id"] = "source-id"
                task = runner.create(workflow, request)
                done = runner.execute(task["task_id"])
                self.assertEqual(done["status"], "COMPLETED")
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(self.calls[0][0], expected_command)
                self.assertEqual(self.calls[0][3], 1)
                self.assertIn("qualification", done["result"])
                self.assertIn("pool", done["result"])
                self.assertEqual(runner.resume(task["task_id"]), done)
                self.assertEqual(len(self.calls), 1)

    def test_empty_regular_campaign_blocks_without_replay(self):
        adapter = self._adapter({"status": "unresolved", "completed": []})
        runner = MainOrchestrator(self.root, adapter=adapter)
        task = runner.create("regular", {"objective": "offline"})
        blocked = runner.execute(task["task_id"])
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["steps"]["execute"], "DONE")
        self.assertEqual(blocked["steps"]["qualify"], "RUNNING")
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(OrchestrationError):
            runner.resume(task["task_id"])
        self.assertEqual(len(self.calls), 1)

    def test_missing_qualification_blocks_without_reexecution(self):
        adapter = self._adapter({"status": "unresolved"})
        runner = MainOrchestrator(self.root, adapter=adapter)
        task = runner.create("super", {"objective": "offline"})
        blocked = runner.execute(task["task_id"])
        self.assertEqual(blocked["status"], "BLOCKED")
        self.assertEqual(blocked["steps"]["execute"], "DONE")
        self.assertEqual(blocked["steps"]["qualify"], "RUNNING")
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(OrchestrationError):
            runner.resume(task["task_id"])
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
