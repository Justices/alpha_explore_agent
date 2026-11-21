"""P4.5 one-shot staging contract; all checks are offline and synthetic."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.simulation_attempt_admission import (
    inspect_staged_simulation_attempt, stage_simulation_attempt,
)


class StagingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.orchestrator = MainOrchestrator(self.root)
        self.task = self.orchestrator.create(
            "super", {"objective": "offline synthetic staging", "region": "USA",
                      "delay": 1, "universe": "TOP3000"})
        self.plan = {
            "research_id": "super:" + "a" * 32,
            "run_id": "b" * 32,
            "batch_id": "one",
            "request": {"selection": "rank(x)", "combo": "rank(y)"},
            "settings": {"region": "USA", "delay": 1, "universe": "TOP3000",
                         "instrument_type": "EQUITY"},
        }
        self.scope_result = {
            "workflow": "super", "authorization_id": "c" * 32,
            "research_id": self.plan["research_id"],
            "simulation_sha256": self._digest(),
            "permission_to_dispatch": False,
        }

    def _digest(self):
        from alpha_research.state.fingerprint import fingerprint_json
        return fingerprint_json({"request": self.plan["request"],
                                 "settings": self.plan["settings"]})

    def stage(self, *, task=None, plan=None, authorization_id=None):
        checked = dict(self.scope_result)
        if authorization_id is not None:
            checked["authorization_id"] = authorization_id
        task_id = self.task["task_id"] if task is None else task["task_id"]
        with patch(
            "alpha_research.orchestration.simulation_attempt_admission.inspect_simulation_scope",
            return_value=checked,
        ):
            return stage_simulation_attempt(
                self.orchestrator, task_id, {}, {}, {"synthetic": True},
                self.plan if plan is None else plan)

    def test_stage_writes_journal_before_intent_and_never_authorizes(self):
        record = self.stage()
        self.assertEqual(record["status"], "STAGED_NO_DISPATCH")
        self.assertFalse(record["permission_to_dispatch"])
        location = self.root / "state" / "research_v1_1" / "simulation_attempt_admission"
        journal = json.loads(
            (location / (self.task["task_id"] + ".json")).read_text())
        intent = json.loads(
            (location / "intents" / (self.task["task_id"] + ".intent.json")).read_text())
        self.assertEqual(journal["status"], "STAGED_NO_DISPATCH")
        self.assertFalse(journal["permission_to_dispatch"])
        self.assertEqual(intent["status"], "PREPARED")
        self.assertIs(intent["metadata"]["dispatch_authorized"], False)
        self.assertEqual(intent["request_sha256"], journal["simulation_sha256"])

    def test_second_stage_same_task_is_rejected(self):
        self.stage()
        with self.assertRaisesRegex(OrchestrationError, "already staged"):
            self.stage()

    def test_reused_authorization_id_is_rejected_for_different_task(self):
        self.stage()
        task_two = self.orchestrator.create(
            "super", {"objective": "another", "region": "USA",
                      "delay": 1, "universe": "TOP3000"})
        with self.assertRaisesRegex(OrchestrationError, "already consumed"):
            self.stage(task=task_two)

    def test_competing_stage_has_one_winner(self):
        def invoke(_):
            try:
                return stage_simulation_attempt(
                    self.orchestrator, self.task["task_id"],
                    {}, {}, {"synthetic": True}, self.plan)["status"]
            except (OrchestrationError, ValueError):
                return "BLOCKED"
        # Patch once around the entire concurrent section. Nesting patch()
        # in each worker races as either thread exits and restores the
        # global target while the other is still verifying its receipt.
        with patch(
            "alpha_research.orchestration.simulation_attempt_admission.inspect_simulation_scope",
            return_value=self.scope_result,
        ):
            with ThreadPoolExecutor(max_workers=2) as pool:
                states = list(pool.map(invoke, range(2)))
        self.assertCountEqual(states, ["STAGED_NO_DISPATCH", "BLOCKED"])

    def test_crash_after_journal_before_intent_requires_reconciliation(self):
        location = self.root / "state" / "research_v1_1" / "simulation_attempt_admission"
        location.mkdir(parents=True)
        (location / (self.task["task_id"] + ".json")).write_text(
            '{"incomplete": true}', encoding="utf-8")
        self.assertEqual(inspect_staged_simulation_attempt(
            self.orchestrator, self.task["task_id"])["status"],
            "RECONCILIATION_REQUIRED")
        with self.assertRaisesRegex(OrchestrationError, "already staged"):
            self.stage()

    def test_orphan_intent_without_journal_requires_reconciliation(self):
        location = (self.root / "state" / "research_v1_1" /
                    "simulation_attempt_admission" / "intents")
        location.mkdir(parents=True)
        (location / (self.task["task_id"] + ".intent.json")).write_text(
            '{"status":"PREPARED"}', encoding="utf-8")
        self.assertEqual(inspect_staged_simulation_attempt(
            self.orchestrator, self.task["task_id"])["status"],
            "RECONCILIATION_REQUIRED")

    def test_global_stop_flag_rejects_stage(self):
        path = self.root / "state" / "stop.flag"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stopped", encoding="utf-8")
        with self.assertRaisesRegex(OrchestrationError, "stop flag"):
            self.stage()


    def test_dangling_journal_symlink_requires_reconciliation(self):
        location = (self.root / "state" / "research_v1_1" /
                    "simulation_attempt_admission")
        location.mkdir(parents=True, exist_ok=True)
        journal = location / (self.task["task_id"] + ".json")
        try:
            journal.symlink_to(location / "missing-journal.json")
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable on this host")
        result = inspect_staged_simulation_attempt(
            self.orchestrator, self.task["task_id"])
        self.assertEqual(result["status"], "RECONCILIATION_REQUIRED")
        self.assertFalse(result["permission_to_dispatch"])
        with self.assertRaisesRegex(OrchestrationError, "already staged"):
            self.stage()

    def test_dangling_stop_symlink_rejects_stage(self):
        stop = self.root / "state" / "stop.flag"
        stop.parent.mkdir(parents=True, exist_ok=True)
        try:
            stop.symlink_to(stop.with_name("missing-stop-target"))
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable on this host")
        with self.assertRaisesRegex(OrchestrationError, "stop flag"):
            self.stage()

    def test_stop_created_during_scope_recheck_blocks_staging(self):
        stop = self.root / "state" / "stop.flag"
        calls = []

        def validate(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                stop.parent.mkdir(parents=True, exist_ok=True)
                stop.write_text("stopped", encoding="utf-8")
            return dict(self.scope_result)

        with patch(
            "alpha_research.orchestration.simulation_attempt_admission.inspect_simulation_scope",
            side_effect=validate,
        ):
            with self.assertRaisesRegex(OrchestrationError, "stop flag"):
                stage_simulation_attempt(
                    self.orchestrator, self.task["task_id"], {}, {},
                    {"synthetic": True}, self.plan)
        self.assertEqual(len(calls), 2)
        journal = (self.root / "state" / "research_v1_1" /
                   "simulation_attempt_admission" /
                   (self.task["task_id"] + ".json"))
        self.assertFalse(journal.exists())

    def test_mock_marker_created_during_scope_recheck_blocks_staging(self):
        from alpha_research.orchestration.mock_workspace import marker_path
        marker = marker_path(self.root)
        calls = []

        def validate(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text('{"kind":"OFFLINE_DOMAIN_MOCK_ONLY"}',
                                  encoding="utf-8")
            return dict(self.scope_result)

        with patch(
            "alpha_research.orchestration.simulation_attempt_admission.inspect_simulation_scope",
            side_effect=validate,
        ):
            with self.assertRaisesRegex(OrchestrationError, "mock workspace"):
                stage_simulation_attempt(
                    self.orchestrator, self.task["task_id"], {}, {},
                    {"synthetic": True}, self.plan)
        self.assertEqual(len(calls), 2)
        journal = (self.root / "state" / "research_v1_1" /
                   "simulation_attempt_admission" /
                   (self.task["task_id"] + ".json"))
        self.assertFalse(journal.exists())


if __name__ == "__main__":
    unittest.main()
