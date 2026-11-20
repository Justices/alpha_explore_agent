"""Offline integration contracts for model routing and durable orchestration.

No provider API, subprocess, or BRAIN gateway is allowed here.
"""
import copy
import tempfile
import unittest
from pathlib import Path

from alpha_research.agents.registry import load_role_profile
from alpha_research.agents.router import RoleRoutedModelClient
from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError, STEPS
from alpha_research.orchestration.offline_adapter import OfflineResearchAdapter


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

    def test_all_workflows_complete_only_from_offline_fixtures(self):
        for workflow in ("regular", "super", "optimization"):
            with self.subTest(workflow=workflow), tempfile.TemporaryDirectory() as td:
                fixtures = {
                    workflow + "." + step: {"step": step}
                    for step in STEPS
                }
                runner = MainOrchestrator(td, adapter=OfflineResearchAdapter(fixtures))
                request = {"objective": "offline validation"}
                if workflow == "optimization":
                    request["alpha_id"] = "synthetic-alpha"
                created = runner.create(workflow, request, budget=1)
                completed = runner.execute(created["task_id"])
                self.assertEqual(completed["status"], "COMPLETED")
                self.assertTrue(all(v == "DONE" for v in completed["steps"].values()))
                self.assertEqual(completed["budget"], {
                    "limit": 1, "used": 1, "reserved": 0,
                })
                self.assertEqual(completed["result"]["qualification"], {
                    "step": "qualify",
                })
                self.assertEqual(runner.load(created["task_id"]), completed)
                self.assertEqual(runner.execute(created["task_id"]), completed)

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

    def test_missing_execute_fixture_blocks_without_automatic_replay(self):
        with tempfile.TemporaryDirectory() as td:
            adapter = OfflineResearchAdapter({
                "regular.validate": {"validated": True},
            })
            runner = MainOrchestrator(td, adapter=adapter)
            created = runner.create("regular", {"objective": "offline failure"}, budget=1)
            result = runner.execute(created["task_id"])
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["failure"]["step"], "execute")
            self.assertEqual(
                result["failure"]["reason"],
                "outcome_unknown_manual_reconciliation_required",
            )
            self.assertEqual(result["budget"]["reserved"], 1)
            self.assertEqual(result["budget"]["used"], 0)
            with self.assertRaisesRegex(OrchestrationError, "explicit reconciliation"):
                runner.resume(created["task_id"])

    def test_missing_adapter_cannot_silently_start_live_work(self):
        with tempfile.TemporaryDirectory() as td:
            runner = MainOrchestrator(td)
            created = runner.create("regular", {"objective": "offline only"})
            with self.assertRaisesRegex(OrchestrationError, "offline adapter is required"):
                runner.execute(created["task_id"])
            self.assertEqual(runner.load(created["task_id"])["status"], "CREATED")


if __name__ == "__main__":
    unittest.main()
