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


    def test_canonical_index_precedes_projection_and_is_reciprocal(self):
        self.stage()
        location = (self.root / "state" / "research_v1_1" /
                    "simulation_attempt_admission")
        index = json.loads((location / "authorization_index.json").read_text())
        task_id = self.task["task_id"]
        authorization_id = self.scope_result["authorization_id"]
        self.assertEqual(index["by_task"], {task_id: authorization_id})
        from alpha_research.state.fingerprint import fingerprint_json
        journal = json.loads((location / (task_id + ".json")).read_text())
        self.assertEqual(index["by_id"], {
            authorization_id: {
                "task_id": task_id,
                "journal_sha256": fingerprint_json(journal),
            }
        })
        self.assertEqual(inspect_staged_simulation_attempt(
            self.orchestrator, task_id)["status"], "STAGED_NO_DISPATCH")

    def test_missing_canonical_index_requires_reconciliation(self):
        self.stage()
        location = (self.root / "state" / "research_v1_1" /
                    "simulation_attempt_admission")
        (location / "authorization_index.json").unlink()
        result = inspect_staged_simulation_attempt(
            self.orchestrator, self.task["task_id"])
        self.assertEqual(result["status"], "RECONCILIATION_REQUIRED")
        with self.assertRaisesRegex(OrchestrationError, "index missing"):
            self.stage()

    def test_crash_after_index_before_projection_fails_closed(self):
        from alpha_research.state.store import atomic_write_json
        from alpha_research.orchestration import simulation_attempt_admission as mod
        target = self.task["task_id"] + ".json"

        def interrupted(path, value):
            if Path(path).name == target:
                raise OSError("simulated disk failure after canonical index")
            return atomic_write_json(path, value)

        with patch.object(mod, "inspect_simulation_scope",
                          return_value=self.scope_result), patch.object(
                              mod, "atomic_write_json", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "simulated disk failure"):
                mod.stage_simulation_attempt(
                    self.orchestrator, self.task["task_id"],
                    {}, {}, {"synthetic": True}, self.plan)
        location = (self.root / "state" / "research_v1_1" /
                    "simulation_attempt_admission")
        index = json.loads((location / "authorization_index.json").read_text())
        self.assertIn(self.scope_result["authorization_id"], index["by_id"])
        status = inspect_staged_simulation_attempt(
            self.orchestrator, self.task["task_id"])
        self.assertEqual(status["status"], "RECONCILIATION_REQUIRED")
        with self.assertRaisesRegex(OrchestrationError, "projection missing"):
            self.stage()

    def test_tampered_request_is_not_accepted_from_original_digest(self):
        self.stage()
        intent_path = (self.root / "state" / "research_v1_1" /
                       "simulation_attempt_admission" / "intents" /
                       (self.task["task_id"] + ".intent.json"))
        saved = json.loads(intent_path.read_text())
        saved["request"]["selection"] = "rank(tampered)"
        intent_path.write_text(json.dumps(saved), encoding="utf-8")
        result = inspect_staged_simulation_attempt(
            self.orchestrator, self.task["task_id"])
        self.assertEqual(result["status"], "RECONCILIATION_REQUIRED")
        self.assertFalse(result["permission_to_dispatch"])

    def test_tampered_intent_run_and_research_id_require_reconciliation(self):
        for field, new in (("run_id", "wrong-run"), ("metadata", {
                "task_id": self.task["task_id"],
                "authorization_id": self.scope_result["authorization_id"],
                "research_id": "super:" + "f" * 32,
                "dispatch_authorized": False})):
            with self.subTest(field=field):
                if not (self.root / "state" / "research_v1_1" /
                        "simulation_attempt_admission" /
                        (self.task["task_id"] + ".json")).exists():
                    self.stage()
                intent_path = (self.root / "state" / "research_v1_1" /
                               "simulation_attempt_admission" / "intents" /
                               (self.task["task_id"] + ".intent.json"))
                original = intent_path.read_text(encoding="utf-8")
                saved = json.loads(original)
                saved[field] = new
                intent_path.write_text(json.dumps(saved), encoding="utf-8")
                self.assertEqual(inspect_staged_simulation_attempt(
                    self.orchestrator, self.task["task_id"])["status"],
                    "RECONCILIATION_REQUIRED")
                intent_path.write_text(original, encoding="utf-8")

    def test_intent_dispatch_permission_cannot_be_elevated(self):
        self.stage()
        intent_path = (self.root / "state" / "research_v1_1" /
                       "simulation_attempt_admission" / "intents" /
                       (self.task["task_id"] + ".intent.json"))
        saved = json.loads(intent_path.read_text())
        saved["metadata"]["dispatch_authorized"] = True
        intent_path.write_text(json.dumps(saved), encoding="utf-8")
        self.assertEqual(inspect_staged_simulation_attempt(
            self.orchestrator, self.task["task_id"])["status"],
            "RECONCILIATION_REQUIRED")

    def test_reciprocal_index_corruption_rejects_any_task(self):
        self.stage()
        location = self.root / "state" / "research_v1_1" / "simulation_attempt_admission"
        path = location / "authorization_index.json"
        body = json.loads(path.read_text())
        body["by_task"][self.task["task_id"]] = "f" * 32
        path.write_text(json.dumps(body), encoding="utf-8")
        second = self.orchestrator.create(
            "super", {"objective": "second", "region": "USA",
                      "delay": 1, "universe": "TOP3000"})
        self.assertEqual(inspect_staged_simulation_attempt(
            self.orchestrator, second["task_id"])["status"],
            "RECONCILIATION_REQUIRED")
        with self.assertRaisesRegex(OrchestrationError, "reciprocal index"):
            self.stage(task=second, authorization_id="e" * 32)

    def test_inspection_never_creates_or_repairs_index(self):
        location = (self.root / "state" / "research_v1_1" /
                    "simulation_attempt_admission")
        result = inspect_staged_simulation_attempt(
            self.orchestrator, self.task["task_id"])
        self.assertEqual(result["status"], "NOT_STAGED")
        self.assertFalse(location.exists())
        self.stage()
        journal = location / (self.task["task_id"] + ".json")
        journal.unlink()
        before = sorted(str(p) for p in location.rglob("*"))
        result = inspect_staged_simulation_attempt(
            self.orchestrator, self.task["task_id"])
        self.assertEqual(result["status"], "RECONCILIATION_REQUIRED")
        self.assertEqual(before, sorted(str(p) for p in location.rglob("*")))



    def test_canonical_index_seals_immutable_staging_identity(self):
        self.stage()
        location = (self.root / "state" / "research_v1_1" /
                    "simulation_attempt_admission")
        path = location / (self.task["task_id"] + ".json")
        body = json.loads(path.read_text())
        body["run_id"] = "different-run-id"
        path.write_text(json.dumps(body), encoding="utf-8")
        result = inspect_staged_simulation_attempt(
            self.orchestrator, self.task["task_id"])
        self.assertEqual(result["status"], "RECONCILIATION_REQUIRED")
        self.assertFalse(result["permission_to_dispatch"])



if __name__ == "__main__":
    unittest.main()
