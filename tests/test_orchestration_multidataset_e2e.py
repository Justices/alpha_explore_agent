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
from alpha_research.orchestration.execution_trace import inspect_execution_trace
from alpha_research.orchestration.execution_trace import inspect_execution_trace


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
            trace = inspect_execution_trace(root, task["task_id"])
            self.assertEqual(trace["status"], "TRACE_VERIFIED")
            event_types = [event["event_type"] for event in trace["events"]]
            self.assertEqual(event_types.count("regular.batch_started"), 2)
            self.assertEqual(event_types.count("regular.batch_completed"), 2)
            self.assertIn("regular.campaign_completed", event_types)
            self.assertEqual(event_types.count("regular.dataset_attribution_completed"), 2)
            self.assertEqual(event_types.count("regular.dataset_review_completed"), 2)
            self.assertEqual(event_types.count("regular.dataset_no_selection"), 2)
            self.assertEqual(event_types.count("regular.dataset_review_started"), 2)
            self.assertEqual(event_types.count("regular.dataset_attribution_started"), 2)
            self.assertEqual(
                {key: row["status"] for key, row in
                 trace["domain_evidence"]["campaign"]["dataset_reviews"].items()},
                {"dataset_alpha": "no_selection", "dataset_beta": "no_selection"},
            )
            self.assertEqual(trace["domain_evidence"]["campaign"]["campaign_id"],
                             execution["campaign_id"])
            review_refs = {row["review_ref"] for row in
                           trace["domain_evidence"]["campaign"]["dataset_reviews"].values()}
            manifest_paths = {row["path"] for row in trace["evidence_manifest"]["files"]}
            self.assertEqual(len(review_refs), 2)
            self.assertTrue(review_refs.issubset(manifest_paths))
            self.assertIsNotNone(completed["research_identity"])
            self.assertIsNotNone(completed["recovery_target"])
            self.assertFalse(any(command in gateway.calls for command in (
                "simulate_batch", "simulate_super", "submit_alpha",
            )))

            trace = inspect_execution_trace(root, task["task_id"])
            self.assertEqual(trace["status"], "TRACE_VERIFIED")
            self.assertEqual(trace["journal_verification"]["status"], "VERIFIED")
            self.assertEqual(trace["journal_verification"]["event_count"],
                             len(trace["events"]))
            event_types = [event["event_type"] for event in trace["events"]]
            self.assertIn("regular.campaign_scheduled", event_types)
            self.assertEqual(event_types.count("regular.batch_started"), 2)
            self.assertEqual(event_types.count("regular.batch_completed"), 2)
            self.assertIn("regular.dataset_review_completed", event_types)
            self.assertIn("regular.campaign_completed", event_types)
            self.assertIn("task.completed", event_types)
            self.assertEqual(
                {row["dataset_id"] for row in trace["domain_evidence"]["campaign"]["completed"]},
                {"dataset_alpha", "dataset_beta"},
            )
            self.assertEqual(len(trace["domain_evidence"]["simulation_intents"]), 2)
            self.assertFalse(trace["evidence_manifest"]["truncated"])
            evidence_names = {row["name"] for row in trace["evidence_manifest"]["files"]}
            self.assertIn("simulation_intent.json", evidence_names)
            self.assertIn(task["task_id"] + ".json", evidence_names)
            self.assertEqual(sum(row["name"] == "review.json" for row in
                                 trace["evidence_manifest"]["files"]), 2)


if __name__ == "__main__":
    unittest.main()
