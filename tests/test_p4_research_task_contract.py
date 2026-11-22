"""P4.24 — fresh Task admission has one contract across research boundaries."""
from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.manifest_builder import record_agent_generated_manifest
from alpha_research.orchestration.research_preparation import prepare_offline_research
from alpha_research.orchestration.research_task_contract import is_fresh_research_task
from alpha_research.orchestration.research_runtime_adapter import OfflineResearchRuntimeAdapter
from alpha_research.orchestration.mock_workspace import claim_mock_workspace
from alpha_research.orchestration.domain_mock import DomainMockGateway, DomainMockModel


class FreshResearchTaskContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runner = MainOrchestrator(self.root)
        self.task = self.runner.create(
            "super", {"objective": "contract boundaries", "region": "USA",
                      "delay": 1, "universe": "TOP3000"})
        self.task_id = self.task["task_id"]

    def test_fresh_unexecuted_super_and_optimization_are_eligible(self):
        self.assertTrue(is_fresh_research_task(self.task))
        optimization = self.runner.create(
            "optimization", {
                "objective": "contract boundaries", "region": "USA",
                "delay": 1, "universe": "TOP3000", "alpha_id": "SOURCE"})
        self.assertTrue(is_fresh_research_task(optimization))
        regular = self.runner.create(
            "regular", {"objective": "regular is not this prep contract"})
        self.assertFalse(is_fresh_research_task(regular))

    def test_any_progress_or_recovery_state_revokes_freshness(self):
        altered = (
            ("status", "RUNNING"),
            ("workflow", "regular"),
            ("steps", {"validate": "DONE", "execute": "PENDING",
                       "qualify": "PENDING", "collect": "PENDING"}),
            ("budget", {"limit": 1, "used": 0, "reserved": 1}),
            ("budget", {"limit": 2, "used": 0, "reserved": 0}),
            ("checkpoints", {"validate": {"status": "DONE"}}),
            ("research_identity", {"research_id": "super:fake"}),
            ("recovery_target", {"kind": "simulation_intent", "ref": "fake"}),
            ("result", {"status": "DONE"}),
            ("failure", {"type": "error"}),
        )
        for field, value in altered:
            with self.subTest(field=field, value=value):
                task = deepcopy(self.task)
                task[field] = value
                self.assertFalse(is_fresh_research_task(task))
        self.assertFalse(is_fresh_research_task(None))
        self.assertFalse(is_fresh_research_task({}))
        # bool subclasses int: equality alone would accept True as limit=1
        # and False as used=0, so the gate must check exact JSON types.
        for field, value in (
            ("schema_version", True),
            ("budget", {"limit": True, "used": False, "reserved": False}),
        ):
            with self.subTest(typed_field=field):
                task = deepcopy(self.task)
                task[field] = value
                self.assertFalse(is_fresh_research_task(task))

    def test_all_three_research_ports_reject_partially_consumed_task(self):
        # A Task that still says CREATED but has an edited step cannot be
        # used to create a Manifest or start a scripted model reservation.
        task = self.runner.load(self.task_id)
        steps = dict(task["steps"])
        steps["validate"] = "DONE"
        self.runner._transition(task, steps=steps)
        self.assertFalse(is_fresh_research_task(self.runner.load(self.task_id)))

        with self.assertRaisesRegex(OrchestrationError, "fresh unexecuted Task"):
            record_agent_generated_manifest(self.runner, self.task_id, {})
        claim_mock_workspace(self.root)
        with self.assertRaisesRegex(OrchestrationError, "fresh unexecuted"):
            prepare_offline_research(self.runner, self.task_id)
        with self.assertRaisesRegex(OrchestrationError, "unexecuted scoped Task"):
            OfflineResearchRuntimeAdapter(
                self.runner, self.task_id,
                DomainMockGateway("USA"), DomainMockModel(),
                {"max_calls": 1, "max_reported_tokens": 4096,
                 "failover": "manual"})
        self.assertFalse(list(self.root.rglob("*simulation_intent.json")))


if __name__ == "__main__":
    unittest.main()
