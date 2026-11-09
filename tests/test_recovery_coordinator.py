"""Offline tests for the unified fail-closed recovery coordinator."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nastu_quant.research.recovery import RecoveryCoordinator, RecoveryError
from nastu_quant.research.simulation_intent import (
    create_simulation_intent, transition_simulation_intent,
)


class FakeAgent:
    def __init__(self, result=None, error=None):
        self.result = result or {"status": "candidate_evaluated", "alpha_ids": ["A1"]}
        self.error = error
        self.calls = 0

    def recover(self, batch_dir):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class FakeGateway:
    def __init__(self):
        self.calls = []

    def call(self, command, **kwargs):
        self.calls.append(command)
        raise AssertionError("coordinator test must not call a live gateway")


class RecoveryCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.gateway = FakeGateway()

    def _regular_target(self, status="SUBMITTING"):
        batch = self.root / "runs" / "batch-001"
        batch.mkdir(parents=True)
        (batch / "prereg.json").write_text('{"variants":[]}', encoding="utf-8")
        (batch / "simulation_intent.json").write_text(json.dumps({
            "status": status,
            "expressions": ["rank(x)"],
            "settings": {"region": "GLB"},
            "runner": {"case_key": "case", "run_id": "run",
                       "batch_id": "batch-001", "label": "production"},
            "prereg_sha256": "a" * 64,
        }), encoding="utf-8")
        if status == "READ_BACK":
            (batch / "results.json").write_text('{"alpha_ids":["A1"]}', encoding="utf-8")
        return batch

    def _simulation_intent(self, workflow="super", status="POSTING"):
        path = self.root / "state" / "research_v1_1" / workflow / "intent.json"
        path.parent.mkdir(parents=True)
        create_simulation_intent(
            path, workflow=workflow, run_id="run-1", batch_id="batch-1",
            request=({"selection": "rank(x)", "combo": "rank(y)"}
                     if workflow == "super" else {"regular": "rank(x)"}),
            settings={"region": "GLB", "delay": 1},
        )
        if status == "POSTING":
            transition_simulation_intent(
                path, expected_status="PREPARED", status="POSTING")
        return path

    def test_regular_recovery_is_journaled_and_idempotent(self):
        batch = self._regular_target()
        agent = FakeAgent()
        coordinator = RecoveryCoordinator(self.root)
        first = coordinator.recover(batch, gateway=self.gateway, agent=agent)
        second = coordinator.recover(batch, gateway=self.gateway, agent=agent)
        self.assertEqual(first, second)
        self.assertEqual(agent.calls, 1)
        journal = coordinator.journal_for(batch)
        self.assertEqual(journal["status"], "COMPLETED")
        self.assertEqual(journal["workflow"], "regular")
        self.assertEqual(self.gateway.calls, [])

    def test_regular_prepared_intent_is_never_submitted_by_recovery(self):
        batch = self._regular_target(status="PREPARED")
        agent = FakeAgent()
        with self.assertRaisesRegex(RecoveryError, "PREPARED"):
            RecoveryCoordinator(self.root).recover(
                batch, gateway=self.gateway, agent=agent)
        self.assertEqual(agent.calls, 0)

    def test_read_back_regular_requires_saved_results(self):
        batch = self._regular_target(status="READ_BACK")
        (batch / "results.json").unlink()
        with self.assertRaisesRegex(RecoveryError, "no saved results"):
            RecoveryCoordinator(self.root).recover(
                batch, gateway=self.gateway, agent=FakeAgent())

    def test_super_and_optimization_use_existing_intent_handler(self):
        for workflow in ("super", "optimization"):
            with self.subTest(workflow=workflow):
                path = self._simulation_intent(workflow)
                calls = []

                def recover_fn(gateway, state_root, intent_path, config=None):
                    calls.append((gateway, state_root, intent_path, config))
                    return {"status": "candidate_evaluated", "workflow": workflow}

                coordinator = RecoveryCoordinator(
                    self.root, simulation_recover=recover_fn)
                result = coordinator.recover(
                    path, gateway=self.gateway, config={"offline": True})
                self.assertEqual(result["status"], "candidate_evaluated")
                self.assertEqual(len(calls), 1)
                self.assertEqual(coordinator.journal_for(path)["status"], "COMPLETED")
                self.assertEqual(self.gateway.calls, [])

    def test_super_prepared_intent_is_rejected_without_handler_call(self):
        path = self._simulation_intent(status="PREPARED")
        calls = []

        def recover_fn(*args, **kwargs):
            calls.append(True)
            return {"status": "unexpected"}

        with self.assertRaisesRegex(RecoveryError, "PREPARED"):
            RecoveryCoordinator(
                self.root, simulation_recover=recover_fn).recover(
                    path, gateway=self.gateway)
        self.assertEqual(calls, [])

    def test_failed_recovery_is_blocked_and_never_replayed(self):
        batch = self._regular_target()
        agent = FakeAgent(error=RuntimeError("injected recovery failure"))
        coordinator = RecoveryCoordinator(self.root)
        with self.assertRaisesRegex(RecoveryError, "blocked"):
            coordinator.recover(batch, gateway=self.gateway, agent=agent)
        with self.assertRaisesRegex(RecoveryError, "replay"):
            coordinator.recover(batch, gateway=self.gateway, agent=agent)
        self.assertEqual(agent.calls, 1)
        self.assertEqual(coordinator.journal_for(batch)["status"], "BLOCKED")

    def test_target_outside_state_root_is_rejected(self):
        outside = Path(tempfile.gettempdir()) / "outside-recovery-target"
        outside.mkdir(exist_ok=True)
        try:
            with self.assertRaisesRegex(RecoveryError, "outside"):
                RecoveryCoordinator(self.root).recover(
                    outside, gateway=self.gateway, agent=FakeAgent())
        finally:
            outside.rmdir()

    def test_immutable_target_change_blocks_recovery_replay(self):
        batch = self._regular_target()
        coordinator = RecoveryCoordinator(self.root)
        agent = FakeAgent()
        coordinator.recover(batch, gateway=self.gateway, agent=agent)
        intent_path = batch / "simulation_intent.json"
        saved = json.loads(intent_path.read_text(encoding="utf-8"))
        saved["settings"]["region"] = "USA"
        intent_path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaisesRegex(RecoveryError, "evidence changed"):
            coordinator.recover(batch, gateway=self.gateway, agent=agent)
        self.assertEqual(agent.calls, 1)


if __name__ == "__main__":
    unittest.main()
