"""Offline fault-injection tests for ledger/journal cross-file crash gaps.

None of the fixtures can access a network, a BRAIN gateway or a real model.
The key safety rule: unresolved budget or unsealed response cannot result in
a new provider request or a verified budget settlement.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_research.integrations.model import DecisionRecorder, ModelError
from alpha_research.integrations import model_journal


POLICY = {"max_calls": 5, "max_reported_tokens": 1000, "failover": "manual"}


class FakeModel:
    config = {"provider": "mock"}
    last_usage = {"input_tokens": 4, "output_tokens": 3}
    last_model = "offline-fixture"
    last_response_text = None
    last_thread_id = None

    def __init__(self):
        self.calls = 0

    def complete_json(self, task, context, *, thread_path=None):
        self.calls += 1
        return {"accepted": True, "task": task, "step": context["step"]}


class ModelTransactionBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.batch = self.root / "batch"

    def recorder(self, model=None):
        model = model or FakeModel()
        recorder = DecisionRecorder(model, self.root, governance=POLICY)
        recorder.begin_session(research_id="R1")
        recorder.bind_batch(self.batch)
        return recorder

    def snapshot(self, recorder):
        return recorder.budget.status("R1")

    def audit(self):
        return list((self.root / "logs" / "model_calls").glob("*.record.json"))

    def manifest(self):
        return json.loads((self.batch / "model_call_manifest.json")
                          .read_text(encoding="utf-8"))["calls"]

    def test_reservation_before_intent_failure_never_dispatches_or_retries(self):
        first_model = FakeModel()
        first = self.recorder(first_model)
        with patch.object(first.journal, "persist_intent",
                          side_effect=RuntimeError("before durable intent")):
            with self.assertRaisesRegex(RuntimeError, "before durable intent"):
                first.complete_json("task", {"step": 1})
        self.assertEqual(first_model.calls, 0)
        self.assertEqual((self.snapshot(first)["attempts"],
                          self.snapshot(first)["unresolved"]), (1, 1))
        self.assertEqual(self.audit(), [])
        second_model = FakeModel()
        second = self.recorder(second_model)
        with self.assertRaisesRegex(ValueError, "unresolved model call intent"):
            second.complete_json("task", {"step": 1})
        self.assertEqual(second_model.calls, 0)
        self.assertEqual(len(self.manifest()), 0)

    def test_orphan_reservation_reports_call_id_without_implicit_settlement(self):
        first = self.recorder()
        with patch.object(first.journal, "persist_intent",
                          side_effect=KeyboardInterrupt("crash after reserve")):
            with self.assertRaisesRegex(KeyboardInterrupt, "after reserve"):
                first.complete_json("task", {"step": 1})
        report = first.budget.status("R1")
        call_ids = report["unresolved_call_ids"]
        self.assertEqual(len(call_ids), 1)
        self.assertEqual(len(call_ids[0]), 32)
        self.assertEqual(report["unknown_call_ids"], [])
        self.assertEqual(self.audit(), [])

        # An explicit operator acknowledgement does not claim that a
        # provider failed or authorize another attempt in this research.
        first.budget.acknowledge_unknown(
            "R1", call_ids[0], reason="No verifiable durable model outcome")
        closed = first.budget.status("R1")
        self.assertEqual(closed["unresolved_call_ids"], [])
        self.assertEqual(closed["unknown_call_ids"], call_ids)
        self.assertTrue(closed["blocked"])
        next_model = FakeModel()
        with self.assertRaisesRegex(ValueError, "unknown model outcome"):
            self.recorder(next_model).complete_json("task", {"step": 1})
        self.assertEqual(next_model.calls, 0)

    def test_crash_between_context_and_global_audit_blocks_new_request(self):
        first_model = FakeModel()
        first = self.recorder(first_model)
        original = model_journal.atomic_write_json

        def interrupt(path, data):
            if str(path).endswith(".record.json"):
                raise KeyboardInterrupt("crash before global audit record")
            return original(path, data)

        with patch.object(model_journal, "atomic_write_json",
                          side_effect=interrupt):
            with self.assertRaisesRegex(KeyboardInterrupt, "global audit"):
                first.complete_json("task", {"step": 1})
        self.assertEqual(first_model.calls, 0)
        self.assertEqual(self.snapshot(first)["unresolved"], 1)
        self.assertEqual(len(list((self.root / "logs" / "model_calls")
                                  .glob("*.context.json"))), 1)
        self.assertEqual(self.audit(), [])
        second_model = FakeModel()
        with self.assertRaisesRegex(ValueError, "unresolved model call intent"):
            self.recorder(second_model).complete_json("task", {"step": 1})
        self.assertEqual(second_model.calls, 0)

    def test_crash_after_global_audit_but_before_batch_manifest_blocks(self):
        first_model = FakeModel()
        first = self.recorder(first_model)
        original = first.journal.sync_batch_calls

        def interrupt(calls, **kwargs):
            if calls:
                raise RuntimeError("before manifest")
            return original(calls, **kwargs)

        with patch.object(first.journal, "sync_batch_calls", side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, "before manifest"):
                first.complete_json("task", {"step": 1})
        self.assertEqual(first_model.calls, 0)
        self.assertEqual(len(self.audit()), 1)
        self.assertEqual(len(self.manifest()), 0)
        second_model = FakeModel()
        with self.assertRaisesRegex(ValueError, "unresolved model call intent"):
            self.recorder(second_model).complete_json("task", {"step": 1})
        self.assertEqual(second_model.calls, 0)

    def test_crash_after_manifest_before_intent_event_does_not_dispatch(self):
        first_model = FakeModel()
        first = self.recorder(first_model)
        with patch.object(first.journal, "append_event",
                          side_effect=KeyboardInterrupt("intent event interrupted")):
            with self.assertRaisesRegex(KeyboardInterrupt, "event interrupted"):
                first.complete_json("task", {"step": 1})
        self.assertEqual(first_model.calls, 0)
        self.assertEqual(len(self.manifest()), 1)
        self.assertEqual(self.snapshot(first)["unresolved"], 1)
        second_model = FakeModel()
        with self.assertRaisesRegex(ModelError, "retry blocked"):
            self.recorder(second_model).complete_json("task", {"step": 1})
        self.assertEqual(second_model.calls, 0)

    def test_crash_after_model_before_response_durable_keeps_unknown(self):
        first_model = FakeModel()
        first = self.recorder(first_model)
        original = model_journal.atomic_write_json

        def interrupt(path, data):
            if str(path).endswith(".response.json"):
                raise KeyboardInterrupt("before response persisted")
            return original(path, data)

        with patch.object(model_journal, "atomic_write_json",
                          side_effect=interrupt):
            with self.assertRaisesRegex(KeyboardInterrupt, "response persisted"):
                first.complete_json("task", {"step": 1})
        self.assertEqual(first_model.calls, 1)
        self.assertEqual(self.snapshot(first)["unresolved"], 1)
        second_model = FakeModel()
        with self.assertRaisesRegex(ModelError, "retry blocked"):
            self.recorder(second_model).complete_json("task", {"step": 1})
        self.assertEqual(second_model.calls, 0)

    def terminal_audit_gap(self):
        first_model = FakeModel()
        first = self.recorder(first_model)
        original = model_journal.atomic_write_json

        def interrupt(path, data):
            if str(path).endswith(".record.json") and data.get("status") == "ok":
                raise KeyboardInterrupt("terminal audit interrupted")
            return original(path, data)

        with patch.object(model_journal, "atomic_write_json",
                          side_effect=interrupt):
            with self.assertRaisesRegex(KeyboardInterrupt, "terminal audit"):
                first.complete_json("task", {"step": 1})
        self.assertEqual(first_model.calls, 1)
        self.assertEqual(self.snapshot(first)["unresolved"], 1)
        row, = self.manifest()
        response = Path(row["global_response_ref"])
        self.assertTrue(response.exists())
        self.assertEqual(
            json.loads(response.read_text(encoding="utf-8"))["status"], "ok")
        self.assertIsNone(row["response_sha256"])
        return response

    def test_sealed_response_recovers_unsettled_budget_without_new_call(self):
        response = self.terminal_audit_gap()
        self.assertRegex(response.read_text(encoding="utf-8"),
                         r'"state_sha256"')
        next_model = FakeModel()
        resumed = self.recorder(next_model)
        result = resumed.complete_json("task", {"step": 1})
        self.assertEqual(result, {"accepted": True, "task": "task", "step": 1})
        self.assertEqual(next_model.calls, 0)
        self.assertEqual((self.snapshot(resumed)["successful"],
                          self.snapshot(resumed)["unresolved"]), (1, 0))
        resumed.complete_json("task", {"step": 1})
        self.assertEqual(next_model.calls, 0)
        self.assertEqual(self.snapshot(resumed)["attempts"], 1)

    def test_tampered_unsettled_response_refuses_budget_reconciliation(self):
        response = self.terminal_audit_gap()
        data = json.loads(response.read_text(encoding="utf-8"))
        data["result"]["accepted"] = False
        response.write_text(json.dumps(data), encoding="utf-8")
        next_model = FakeModel()
        resumed = self.recorder(next_model)
        with self.assertRaisesRegex(ModelError, "integrity seal"):
            resumed.complete_json("task", {"step": 1})
        self.assertEqual(next_model.calls, 0)
        self.assertEqual(self.snapshot(resumed)["unresolved"], 1)

    def test_unsealed_valid_looking_response_never_settles_budget(self):
        response = self.terminal_audit_gap()
        data = json.loads(response.read_text(encoding="utf-8"))
        del data["state_sha256"]
        response.write_text(json.dumps(data), encoding="utf-8")
        next_model = FakeModel()
        resumed = self.recorder(next_model)
        with self.assertRaisesRegex(ModelError, "integrity seal"):
            resumed.complete_json("task", {"step": 1})
        self.assertEqual(next_model.calls, 0)
        self.assertEqual(self.snapshot(resumed)["unresolved"], 1)


if __name__ == "__main__":
    unittest.main()
