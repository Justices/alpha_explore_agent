"""Offline crash-gap tests: durable response versus unsettled budget.

Uses only a deterministic fake model, isolated temporary files, and no BRAIN
or external model service. These tests never submit or simulate an Alpha.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_research.agents.governance import ModelBudgetError, ModelCallLedger
from alpha_research.integrations.model import DecisionRecorder, ModelError


POLICY = {"max_calls": 5, "max_reported_tokens": 1000, "failover": "manual"}


class CountingModel:
    config = {"provider": "mock"}

    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail
        self.last_usage = {"input_tokens": 7, "output_tokens": 3}
        self.last_model = "offline"
        self.last_response_text = None
        self.last_thread_id = None

    def complete_json(self, task, context, *, thread_path=None):
        self.calls += 1
        if self.fail:
            raise TimeoutError("synthetic failed model call")
        return {"task": task, "step": context["step"]}


class BudgetRecoveryTests(unittest.TestCase):
    def make_recorder(self, root, model, research_id="R1"):
        recorder = DecisionRecorder(model, root, governance=POLICY)
        recorder.begin_session(research_id=research_id)
        recorder.bind_batch(root / "batch")
        return recorder

    def create_gap(self, root, *, fail=False):
        model = CountingModel(fail=fail)
        first = self.make_recorder(root, model)
        # Model finished and audit files are durable; only budget settlement
        # fails, modeling a process crash precisely at that boundary.
        with patch.object(first.budget, "settle",
                          side_effect=RuntimeError("crash before ledger settlement")):
            with self.assertRaisesRegex(RuntimeError, "crash before ledger settlement"):
                first.complete_json("task", {"step": 1})
        self.assertEqual(model.calls, 1)
        status = first.budget.status("R1")
        self.assertEqual((status["attempts"], status["unresolved"]), (1, 1))
        return model

    def test_successful_persisted_response_settles_on_replay_without_another_call(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            first = self.create_gap(root)
            resumed_model = CountingModel()
            resumed = self.make_recorder(root, resumed_model)
            self.assertEqual(resumed.complete_json("task", {"step": 1}),
                             {"task": "task", "step": 1})
            self.assertEqual((first.calls, resumed_model.calls), (1, 0))
            status = resumed.budget.status("R1")
            self.assertEqual((status["attempts"], status["successful"],
                              status["unresolved"], status["reported_tokens"]),
                             (1, 1, 0, 10))
            # Repeated replay must not count or settle a second time.
            self.assertEqual(resumed.complete_json("task", {"step": 1}),
                             {"task": "task", "step": 1})
            self.assertEqual(resumed.budget.status("R1")["attempts"], 1)

    def test_reconciled_research_can_reserve_the_next_call(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.create_gap(root)
            resumed = self.make_recorder(root, CountingModel())
            resumed.complete_json("task", {"step": 1})
            self.assertEqual(resumed.complete_json("task", {"step": 2}),
                             {"task": "task", "step": 2})
            report = resumed.budget.status("R1")
            self.assertEqual((report["attempts"], report["successful"],
                              report["unresolved"]), (2, 2, 0))

    def test_error_receipt_from_real_provider_settles_without_retry(self):
        class RealModel(CountingModel):
            config = {"provider": "openai_compatible", "model": "fixture"}

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            first_model = RealModel(fail=True)
            first = self.make_recorder(root, first_model)
            with patch.object(first.budget, "settle",
                              side_effect=RuntimeError("crash after stored error")):
                with self.assertRaisesRegex(RuntimeError, "crash after stored error"):
                    first.complete_json("task", {"step": 1})
            next_model = RealModel(fail=True)
            replay = self.make_recorder(root, next_model)
            with self.assertRaisesRegex(ModelError, "explicit state reconciliation"):
                replay.complete_json("task", {"step": 1})
            self.assertEqual((first_model.calls, next_model.calls), (1, 0))
            report = replay.budget.status("R1")
            self.assertEqual((report["attempts"], report["failed"],
                              report["unresolved"]), (1, 1, 0))

    def test_different_research_id_cannot_recover_other_studys_budget(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.create_gap(root)
            model = CountingModel()
            replay = self.make_recorder(root, model, research_id="R2")
            with self.assertRaisesRegex(ModelError, "audit identity mismatch"):
                replay.complete_json("task", {"step": 1})
            self.assertEqual(model.calls, 0)
            self.assertEqual(replay.budget.status("R1")["unresolved"], 1)
            self.assertEqual(replay.budget.status("R2")["attempts"], 0)

    def test_saved_response_repairs_in_flight_audit_and_budget(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.create_gap(root)
            manifest = json.loads((root / "batch" / "model_call_manifest.json")
                                  .read_text(encoding="utf-8"))["calls"][0]
            audit_path = Path(manifest["global_record_ref"])
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            audit["status"] = "in_flight"
            audit["response_sha256"] = None
            audit["token_in"] = None
            audit["token_out"] = None
            audit_path.write_text(json.dumps(audit), encoding="utf-8")
            next_model = CountingModel()
            recovered = self.make_recorder(root, next_model)
            self.assertEqual(recovered.complete_json("task", {"step": 1})["step"], 1)
            self.assertEqual(next_model.calls, 0)
            self.assertEqual(recovered.budget.status("R1")["successful"], 1)
            repaired = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(repaired["status"], "ok")
            self.assertTrue(repaired["response_sha256"])

    def test_backend_change_is_rejected_during_budget_recovery(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.create_gap(root)
            changed_model = CountingModel()
            changed_model.config = {"provider": "mock", "model": "changed-backend"}
            replay = self.make_recorder(root, changed_model)
            with self.assertRaisesRegex(ModelBudgetError, "reserved model identity"):
                replay.complete_json("task", {"step": 1})
            self.assertEqual(changed_model.calls, 0)
            self.assertEqual(replay.budget.status("R1")["unresolved"], 1)

    def test_ledger_rejection_does_not_rewrite_in_flight_audit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.create_gap(root)
            row = json.loads((root / "batch" / "model_call_manifest.json")
                             .read_text(encoding="utf-8"))["calls"][0]
            audit_path = Path(row["global_record_ref"])
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            audit.update(status="in_flight", response_sha256=None,
                         token_in=None, token_out=None)
            audit_path.write_text(json.dumps(audit), encoding="utf-8")
            changed_model = CountingModel()
            changed_model.config = {"provider": "mock", "model": "unexpected"}
            replay = self.make_recorder(root, changed_model)
            with self.assertRaisesRegex(ModelBudgetError, "reserved model identity"):
                replay.complete_json("task", {"step": 1})
            after = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(after["status"], "in_flight")
            self.assertEqual(changed_model.calls, 0)

    def test_partial_in_flight_audit_usage_blocks_reconciliation(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.create_gap(root)
            row = json.loads((root / "batch" / "model_call_manifest.json")
                             .read_text(encoding="utf-8"))["calls"][0]
            record_path = Path(row["global_record_ref"])
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record.update(status="in_flight", response_sha256=None, token_in=3)
            record_path.write_text(json.dumps(record), encoding="utf-8")
            next_model = CountingModel()
            replay = self.make_recorder(root, next_model)
            with self.assertRaisesRegex(ModelError, "in-flight model audit has invalid usage"):
                replay.complete_json("task", {"step": 1})
            self.assertEqual(next_model.calls, 0)
            self.assertEqual(replay.budget.status("R1")["unresolved"], 1)

    def test_crash_before_terminal_manifest_sync_uses_global_response_once(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            initial_model = CountingModel()
            first = self.make_recorder(root, initial_model)
            original_sync = first.journal.sync_batch_calls

            def interrupt_final_sync(calls, *, batch_dir, session_id):
                # The global response and terminal audit have been persisted,
                # but the batch manifest still contains the initial intent.
                if calls and calls[0][0]["status"] == "ok":
                    raise RuntimeError("crash before terminal batch sync")
                return original_sync(calls, batch_dir=batch_dir,
                                     session_id=session_id)

            with patch.object(first.journal, "sync_batch_calls",
                              side_effect=interrupt_final_sync):
                with self.assertRaisesRegex(RuntimeError,
                                            "crash before terminal batch sync"):
                    first.complete_json("task", {"step": 1})
            manifest = json.loads((root / "batch" / "model_call_manifest.json")
                                  .read_text(encoding="utf-8"))["calls"][0]
            self.assertIsNone(manifest["response_sha256"])
            self.assertFalse(Path(manifest["response_ref"]).exists())
            self.assertTrue(Path(manifest["global_response_ref"]).exists())
            self.assertEqual(first.budget.status("R1")["unresolved"], 1)

            recovered_model = CountingModel()
            recovered = self.make_recorder(root, recovered_model)
            self.assertEqual(recovered.complete_json("task", {"step": 1}),
                             {"task": "task", "step": 1})
            self.assertEqual((initial_model.calls, recovered_model.calls), (1, 0))
            report = recovered.budget.status("R1")
            self.assertEqual((report["attempts"], report["successful"],
                              report["unresolved"]), (1, 1, 0))

    def test_missing_response_never_settles_ledger_or_reissues_model_call(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.create_gap(root)
            row = json.loads((root / "batch" / "model_call_manifest.json")
                             .read_text(encoding="utf-8"))["calls"][0]
            Path(row["response_ref"]).unlink()
            Path(row["global_response_ref"]).unlink()
            next_model = CountingModel()
            replay = self.make_recorder(root, next_model)
            with self.assertRaisesRegex(ModelError, "retry blocked"):
                replay.complete_json("task", {"step": 1})
            self.assertEqual(next_model.calls, 0)
            self.assertEqual(replay.budget.status("R1")["unresolved"], 1)

    def test_recorded_usage_conflict_does_not_settle_budget(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.create_gap(root)
            row = json.loads((root / "batch" / "model_call_manifest.json")
                             .read_text(encoding="utf-8"))["calls"][0]
            record_path = Path(row["global_record_ref"])
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["token_in"] = 999
            record_path.write_text(json.dumps(record), encoding="utf-8")
            model = CountingModel()
            replay = self.make_recorder(root, model)
            with self.assertRaisesRegex(ModelError, "usage mismatch"):
                replay.complete_json("task", {"step": 1})
            self.assertEqual(model.calls, 0)
            self.assertEqual(replay.budget.status("R1")["unresolved"], 1)

    def test_unknown_operator_acknowledgement_is_terminal(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.create_gap(root)
            ledger = ModelCallLedger(root, POLICY)
            state_path, _ = ledger._paths("R1")
            call_id = json.loads(state_path.read_text(encoding="utf-8"))["calls"][0]["call_id"]
            ledger.acknowledge_unknown("R1", call_id,
                                       reason="Operator cannot verify remote outcome")
            model = CountingModel()
            replay = self.make_recorder(root, model)
            with self.assertRaisesRegex(ModelBudgetError, "cannot be reconciled"):
                replay.complete_json("task", {"step": 1})
            self.assertEqual(model.calls, 0)
            self.assertEqual(ledger.status("R1")["unknown"], 1)

    def test_verified_reconciliation_cannot_change_settled_identity_or_usage(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            ledger = ModelCallLedger(root, POLICY)
            ledger.reserve("R1", "call1", task="task", backend_sha256="a" * 64)
            ledger.settle("R1", "call1", success=True, token_in=7, token_out=3)
            args = dict(task="task", role=None, backend=None,
                        backend_sha256="a" * 64, success=True,
                        token_in=7, token_out=3)
            ledger.reconcile_verified_response("R1", "call1", **args)
            self.assertEqual(ledger.status("R1")["attempts"], 1)
            with self.assertRaisesRegex(ModelBudgetError, "usage conflicts"):
                ledger.reconcile_verified_response("R1", "call1",
                                                   **dict(args, token_out=4))
            with self.assertRaisesRegex(ModelBudgetError, "reserved model identity"):
                ledger.reconcile_verified_response("R1", "call1",
                                                   **dict(args, backend_sha256="b" * 64))
            with self.assertRaisesRegex(ModelBudgetError, "settled model outcome"):
                ledger.reconcile_verified_response("R1", "call1",
                                                   **dict(args, success=False))

    def test_unverified_gap_still_blocks_new_calls(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.create_gap(root)
            next_model = CountingModel()
            resumed = self.make_recorder(root, next_model)
            with self.assertRaisesRegex(ModelBudgetError, "unresolved model call intent"):
                resumed.complete_json("task", {"step": 2})
            self.assertEqual(next_model.calls, 0)


if __name__ == "__main__":
    unittest.main()
