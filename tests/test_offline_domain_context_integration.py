"""Offline control-plane integration across real Dispatch and recovery target."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_research.config import DEFAULT_CONFIG
from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.offline_adapter import MockWorkflowAdapter


class FakeGateway:
    is_offline_mock = True


class FakeModel:
    is_offline_mock = True


class OfflineDomainContextIntegrationTests(unittest.TestCase):
    def test_all_workflows_forward_identity_and_intent_through_actual_dispatch(self):
        functions = {
            "regular": "run_regular_campaign",
            "super": "run_super_research",
            "optimization": "run_optimization",
        }
        outputs = {
            "regular": {"status": "budget_complete", "completed": [
                {"alpha_ids": ["offline-A"], "candidate_status": {"offline-A": "CANDIDATE"}}]},
            "super": {"candidate_id": "offline-S", "candidate_status": "CANDIDATE",
                      "preferred_status": "NOT_PREFERRED", "candidate_ref": "c.json",
                      "preferred_ref": "p.json"},
            "optimization": {"candidate_id": "offline-O", "candidate_status": "CANDIDATE",
                             "preferred_status": "NOT_PREFERRED", "candidate_ref": "c.json",
                             "preferred_ref": "p.json"},
        }
        for workflow, func in functions.items():
            with self.subTest(workflow=workflow), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                agent = type("Agent", (), {"model": FakeModel()})()
                adapter = MockWorkflowAdapter(root, DEFAULT_CONFIG, FakeGateway(), agent)
                runner = MainOrchestrator(root, adapter=adapter)
                request = {"objective": "offline domain handoff"}
                if workflow == "optimization":
                    request["alpha_id"] = "synthetic-source"
                task = runner.create(workflow, request)
                expected_id = {
                    "regular": "regular:campaign:",
                    "super": "super:",
                    "optimization": "optimization:",
                }[workflow] + "a" * 32
                calls = []
                targets = []

                def fake_domain(*args, **kwargs):
                    calls.append(1)
                    context = kwargs["execution_context"]
                    context["bind_research_identity"](expected_id)
                    context["bind_research_identity"](expected_id)
                    if workflow == "regular":
                        target = root / "state" / "research_v1_1" / "mock-batch"
                        target.mkdir(parents=True)
                        (target / "simulation_intent.json").write_text(
                            json.dumps({"workflow": "regular"}), encoding="utf-8")
                        context["bind_recovery_target"]("regular_batch", target)
                    else:
                        target = root / "state" / "research_v1_1" / (
                            workflow + "-simulation_intent.json")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(json.dumps({"workflow": workflow}), encoding="utf-8")
                        context["bind_recovery_target"]("simulation_intent", target)
                    targets.append(target.resolve())
                    return outputs[workflow]

                with patch("alpha_research.orchestration.dispatch." + func,
                           side_effect=fake_domain):
                    done = runner.execute(task["task_id"])
                self.assertEqual(done["status"], "COMPLETED")
                self.assertEqual(done["research_identity"]["research_id"], expected_id)
                self.assertEqual(done["recovery_target"]["ref"], str(targets[0]))
                self.assertEqual(done["budget"]["reserved"], 0)
                self.assertEqual(done["budget"]["used"], 1)
                self.assertEqual(len(calls), 1)
                self.assertEqual(runner.execute(task["task_id"]), done)

    def test_unknown_outcome_keeps_identity_intent_and_reserved_budget(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agent = type("Agent", (), {"model": FakeModel()})()
            adapter = MockWorkflowAdapter(root, DEFAULT_CONFIG, FakeGateway(), agent)
            runner = MainOrchestrator(root, adapter=adapter)
            task = runner.create("super", {"objective": "mock failure"})
            intent_path = root / "state" / "research_v1_1" / "unknown_simulation_intent.json"
            intent_path.parent.mkdir(parents=True, exist_ok=True)
            intent_path.write_text(json.dumps({"workflow": "super"}), encoding="utf-8")
            invoked = []

            def mock_super(*args, **kwargs):
                invoked.append(1)
                ctx = kwargs["execution_context"]
                ctx["bind_research_identity"]("super:" + "b" * 32)
                ctx["bind_recovery_target"]("simulation_intent", intent_path)
                raise RuntimeError("synthetic unknown provider outcome")

            with patch("alpha_research.orchestration.dispatch.run_super_research",
                       side_effect=mock_super):
                blocked = runner.execute(task["task_id"])
            self.assertEqual(blocked["status"], "BLOCKED")
            self.assertEqual(blocked["budget"]["reserved"], 1)
            self.assertEqual(blocked["budget"]["used"], 0)
            self.assertEqual(blocked["research_identity"]["research_id"], "super:" + "b" * 32)
            self.assertEqual(blocked["recovery_target"]["ref"], str(intent_path.resolve()))
            with self.assertRaisesRegex(OrchestrationError, "explicit reconciliation"):
                runner.resume(task["task_id"])
            self.assertEqual(len(invoked), 1)


if __name__ == "__main__":
    unittest.main()
