"""Offline recovery gate tests. No provider or BRAIN endpoint is reachable."""
import tempfile
import unittest
from pathlib import Path

from alpha_research.agents.registry import (
    bundled_registry_path, load_registry, load_role_profile,
    select_registered_backends,
)
from alpha_research.agents.governance import ModelCallLedger
from alpha_research.orchestration.engine import MainOrchestrator
from alpha_research.orchestration.execution_identity import persist_execution_identity
from alpha_research.orchestration.execution_proposal import persist_execution_proposal
from alpha_research.orchestration.recovery_preflight import inspect_task_recovery_preflight
from alpha_research.simulation.intent import (
    create_simulation_intent, transition_simulation_intent,
)

POLICY = {"max_calls": 5, "max_reported_tokens": 3000, "failover": "manual"}


class InterruptedOfflineDomain:
    def __init__(self, root, *, bind_intent=True, intent_status="POSTING"):
        self.root = Path(root)
        self.bind_intent = bind_intent
        self.intent_status = intent_status
        self.calls = 0

    def run(self, workflow, step, request, checkpoints, context=None):
        if step == "validate":
            return {"validated": True}
        if step == "execute":
            self.calls += 1
            context["bind_research_identity"]("super:" + "a" * 32)
            if self.bind_intent:
                target = self.root / "state" / "research_v1_1" / "test_intent.json"
                create_simulation_intent(
                    target, workflow="super", run_id="a"*32, batch_id="test-batch",
                    request={"selection": "synthetic", "combo": "synthetic"},
                    settings={"region": "CHN"})
                if self.intent_status == "POSTING":
                    transition_simulation_intent(
                        target, expected_status="PREPARED", status="POSTING")
                context["bind_recovery_target"]("simulation_intent", target)
            raise RuntimeError("synthetic interrupted operation")
        return {"done": True}


class RecoveryPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.model = select_registered_backends(
            load_role_profile(), load_registry(bundled_registry_path()),
            assignments={"research_lead": "deepseek",
                         "research_executor": "deepseek"})

    def _blocked(self, **kwargs):
        adapter = InterruptedOfflineDomain(self.root, **kwargs)
        runner = MainOrchestrator(self.root, adapter=adapter)
        task = runner.create("super", {"objective": "offline preflight"})
        persist_execution_proposal(runner, task["task_id"])
        persist_execution_identity(runner, task["task_id"], self.model, POLICY)
        blocked = runner.execute(task["task_id"])
        self.assertEqual(blocked["status"], "BLOCKED")
        return runner, task["task_id"], adapter

    def test_prepared_intent_is_not_recoverable_or_replayed(self):
        runner, task_id, adapter = self._blocked(intent_status="PREPARED")
        report = inspect_task_recovery_preflight(
            runner, task_id, self.model, POLICY)
        self.assertEqual(report["status"], "INTENT_REQUIRES_MANUAL_INSPECTION")
        self.assertFalse(report["automatic_replay_allowed"])
        self.assertEqual(adapter.calls, 1)

    def test_posting_intent_requires_manual_platform_reconciliation(self):
        runner, task_id, adapter = self._blocked()
        report = inspect_task_recovery_preflight(
            runner, task_id, self.model, POLICY)
        self.assertEqual(report["status"], "PLATFORM_RECONCILIATION_REQUIRED")
        self.assertEqual(report["intent_status"], "POSTING")
        self.assertEqual(report["budget_reserved"], 1)
        self.assertFalse(report["automatic_replay_allowed"])
        self.assertEqual(adapter.calls, 1)

    def test_unknown_model_attempt_blocks_platform_readiness(self):
        runner, task_id, adapter = self._blocked()
        ledger = ModelCallLedger(self.root, POLICY)
        ledger.reserve("super:" + "a" * 32, "test-call", task="construct_super",
                       role="research_lead", backend="registry:deepseek",
                       backend_sha256="b"*64)
        report = inspect_task_recovery_preflight(
            runner, task_id, self.model, POLICY)
        self.assertEqual(report["status"], "MODEL_ATTEMPT_RECONCILIATION_REQUIRED")
        self.assertEqual(report["model_unresolved"], 1)
        self.assertEqual(adapter.calls, 1)

    def test_missing_target_is_not_automatically_replayed(self):
        runner, task_id, adapter = self._blocked(bind_intent=False)
        report = inspect_task_recovery_preflight(
            runner, task_id, self.model, POLICY)
        self.assertEqual(report["status"], "MISSING_DURABLE_INTENT")
        self.assertFalse(report["automatic_replay_allowed"])
        self.assertEqual(adapter.calls, 1)


if __name__ == "__main__":
    unittest.main()
