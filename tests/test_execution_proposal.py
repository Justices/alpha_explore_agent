"""Offline execution proposal tests; never invoke BRAIN or a model."""
import copy
import tempfile
import unittest
from pathlib import Path

from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.execution_proposal import (
    build_execution_proposal, persist_execution_proposal, inspect_execution_proposal,
)
from alpha_research.state.store import atomic_write_json


class ExecutionProposalTests(unittest.TestCase):
    def test_proposal_pins_task_identity_and_never_authorizes_external_calls(self):
        for workflow in ("regular", "super", "optimization"):
            with self.subTest(workflow=workflow), tempfile.TemporaryDirectory() as td:
                runner = MainOrchestrator(td)
                request = {"objective": "no live calls", "region": "CHN"}
                if workflow == "optimization":
                    request["alpha_id"] = "offline-source"
                task = runner.create(workflow, request)
                record = persist_execution_proposal(runner, task["task_id"])
                self.assertEqual(record, inspect_execution_proposal(runner, task["task_id"]))
                self.assertEqual(record, persist_execution_proposal(runner, task["task_id"]))
                proposal = record["proposal"]
                self.assertEqual(proposal["status"], "PROPOSED_NOT_AUTHORIZED")
                self.assertEqual(proposal["task_id"], task["task_id"])
                self.assertEqual(proposal["request_sha256"], task["request_sha256"])
                self.assertIsNone(proposal["authorization"])
                for action in ("allow_model_call", "allow_brain_simulation",
                               "allow_alpha_submission"):
                    self.assertIs(proposal[action], False)
                self.assertEqual(runner.load(task["task_id"]), task)

    def test_reject_modified_proposal_and_task(self):
        with tempfile.TemporaryDirectory() as td:
            runner = MainOrchestrator(td)
            task = runner.create("regular", {"objective": "unchanged"})
            persist_execution_proposal(runner, task["task_id"])
            path = (Path(td) / "state" / "research_v1_1"
                    / "execution_proposals" / (task["task_id"] + ".json"))
            record = inspect_execution_proposal(runner, task["task_id"])
            changed = copy.deepcopy(record)
            changed["proposal"]["allow_brain_simulation"] = True
            atomic_write_json(path, changed)
            with self.assertRaisesRegex(OrchestrationError, "fingerprint"):
                inspect_execution_proposal(runner, task["task_id"])
            with self.assertRaisesRegex(OrchestrationError, "changed"):
                persist_execution_proposal(runner, task["task_id"])
            atomic_write_json(path, record)
            task_file = runner._path(task["task_id"])
            bad_task = runner.load(task["task_id"])
            bad_task["request"]["objective"] = "changed"
            atomic_write_json(task_file, bad_task)
            with self.assertRaises(OrchestrationError):
                inspect_execution_proposal(runner, task["task_id"])

    def test_non_created_task_and_budget_not_admitted(self):
        with tempfile.TemporaryDirectory() as td:
            runner = MainOrchestrator(td)
            task = runner.create("regular", {"objective": "blocked"})
            bad = copy.deepcopy(task)
            bad["status"] = "RUNNING"
            with self.assertRaisesRegex(OrchestrationError, "unstarted"):
                build_execution_proposal(bad)
            bad = copy.deepcopy(task)
            bad["budget"]["limit"] = 2
            with self.assertRaisesRegex(OrchestrationError, "one-batch"):
                build_execution_proposal(bad)
            bad = copy.deepcopy(task)
            bad["request"]["max_batches"] = True
            with self.assertRaises(OrchestrationError):
                build_execution_proposal(bad)
            bad = copy.deepcopy(task)
            bad["recovery_target"] = {"kind": "simulation_intent"}
            with self.assertRaisesRegex(OrchestrationError, "recovery state"):
                build_execution_proposal(bad)


if __name__ == "__main__":
    unittest.main()
