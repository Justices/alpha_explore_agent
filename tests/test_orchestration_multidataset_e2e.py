"""End-to-end regression for the existing Regular multi-Dataset Campaign path.

This exercises MainOrchestrator -> MockWorkflowAdapter -> real dispatch ->
real Campaign scheduler, with only the individual batch boundary scripted.
No live Provider, BRAIN session, Simulation POST or Alpha submission is used.
"""
import tempfile
import unittest
from pathlib import Path

from alpha_research.orchestration.domain_mock import (
    build_domain_mock_adapter, mock_domain_boundary,
)
from alpha_research.orchestration.engine import MainOrchestrator


class RegularMultiDatasetOrchestrationTests(unittest.TestCase):
    def test_campaign_executes_one_bounded_batch_per_pinned_dataset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            request = {
                "objective": "offline multi-Dataset acceptance",
                "region": "USA",
                "delay": 1,
                "universe": "TOP3000",
                "dataset_ids": ["dataset_alpha", "dataset_beta"],
                "category": "OTHER",
                "ppa_profile": False,
            }
            runner = MainOrchestrator(root)
            task = runner.create("regular", request, budget=1)
            adapter, gateway, agent = build_domain_mock_adapter(root, task)
            runner.adapter = adapter

            with mock_domain_boundary("USA"):
                completed = runner.execute(task["task_id"])

            self.assertEqual(completed["status"], "COMPLETED")
            self.assertEqual(completed["steps"], {
                "validate": "DONE", "execute": "DONE",
                "qualify": "DONE", "collect": "DONE",
            })
            self.assertEqual(completed["budget"], {
                "limit": 1, "used": 1, "reserved": 0,
            })
            execution = completed["checkpoints"]["execute"]
            self.assertEqual(execution["status"], "budget_complete")
            self.assertEqual(len(execution["completed"]), 2)
            self.assertEqual(
                {row["dataset_id"] for row in execution["completed"]},
                {"dataset_alpha", "dataset_beta"},
            )
            self.assertEqual(agent.calls, 2)
            self.assertIsNotNone(completed["research_identity"])
            self.assertIsNotNone(completed["recovery_target"])
            self.assertFalse(any(command in gateway.calls for command in (
                "simulate_batch", "simulate_super", "submit_alpha",
            )))


if __name__ == "__main__":
    unittest.main()
