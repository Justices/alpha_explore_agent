"""Offline integration contracts for model routing and durable orchestration.

No provider API, subprocess, or BRAIN gateway is allowed here.
"""
import tempfile
import unittest

from alpha_research.agents.registry import load_role_profile
from alpha_research.agents.router import RoleRoutedModelClient
from alpha_research.orchestration.engine import MainOrchestrator


class OfflineModelOrchestrationTests(unittest.TestCase):
    def test_model_tasks_route_to_distinct_mock_roles(self):
        profile = load_role_profile()
        profile["roles"]["research_lead"]["backend"] = "offline-lead"
        profile["roles"]["research_executor"]["backend"] = "offline-executor"
        profile["backends"] = {
            "offline-lead": {
                "kind": "mock",
                "provider": "mock",
                "responses": {"construct_batch": {"source": "lead"}},
            },
            "offline-executor": {
                "kind": "mock",
                "provider": "mock",
                "responses": {"attribute_results": {"source": "executor"}},
            },
        }
        router = RoleRoutedModelClient(profile)
        self.assertEqual(router.role_for("construct_batch"), "research_lead")
        self.assertEqual(router.role_for("attribute_results"), "research_executor")
        self.assertEqual(
            router.complete_json("construct_batch", {})["source"], "lead")
        self.assertEqual(
            router.complete_json("attribute_results", {})["source"], "executor")
        self.assertEqual(len(router.instances), 2)

    def test_lead_executor_handoff_with_durable_offline_checkpoints(self):
        profile = load_role_profile()
        profile["roles"]["research_lead"]["backend"] = "offline-lead"
        profile["roles"]["research_executor"]["backend"] = "offline-executor"
        profile["backends"] = {
            "offline-lead": {
                "kind": "mock", "provider": "mock",
                "responses": {"construct_batch": {"hypothesis": "synthetic"}},
            },
            "offline-executor": {
                "kind": "mock", "provider": "mock",
                "responses": {"attribute_results": {"analysis": "synthetic-only"}},
            },
        }
        router = RoleRoutedModelClient(profile)

        class RoleHandoffAdapter:
            def run(self, workflow, step, request, checkpoints, context=None):
                if step == "validate":
                    return {"validated": True}
                if step == "execute":
                    return router.complete_json("construct_batch", request)
                if step == "qualify":
                    if checkpoints["execute"]["hypothesis"] != "synthetic":
                        raise AssertionError("missing Lead checkpoint")
                    return router.complete_json("attribute_results", checkpoints["execute"])
                if step == "collect":
                    return {"lead": checkpoints["execute"],
                            "executor": checkpoints["qualify"]}
                raise AssertionError("unexpected step")

        with tempfile.TemporaryDirectory() as td:
            runner = MainOrchestrator(td, adapter=RoleHandoffAdapter())
            task = runner.create("regular", {"objective": "offline role handoff"})
            completed = runner.execute(task["task_id"])
            self.assertEqual(completed["status"], "COMPLETED")
            self.assertEqual(completed["result"], {
                "lead": {"hypothesis": "synthetic"},
                "executor": {"analysis": "synthetic-only"},
            })
            saved = runner.load(task["task_id"])
            self.assertEqual(saved["checkpoints"]["execute"]["hypothesis"], "synthetic")
            self.assertEqual(saved["checkpoints"]["qualify"]["analysis"], "synthetic-only")
            self.assertEqual(set(router.instances), {"offline-lead", "offline-executor"})



if __name__ == "__main__":
    unittest.main()
