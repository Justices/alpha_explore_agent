"""Offline boundary tests for durable model-call journaling and replay.

No provider transport, CLI, SDK, or BRAIN gateway is used.
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from alpha_research.integrations.model import DecisionRecorder, ModelError


class CountingModel:
    config = {"provider": "mock"}

    def __init__(self):
        self.calls = 0
        self.last_usage = {"input_tokens": 3, "output_tokens": 2}
        self.last_model = "offline-counting"
        self.last_thread_id = None
        self.last_response_text = None

    def complete_json(self, task, context, *, thread_path=None):
        self.calls += 1
        return {"task": task, "round": context["round"]}


class JournalReplayTests(unittest.TestCase):
    @staticmethod
    def create_call(root):
        first_model = CountingModel()
        recorder = DecisionRecorder(first_model, root)
        recorder.begin_session(research_id="offline-research")
        batch = root / "batch"
        recorder.bind_batch(batch)
        expected = recorder.complete_json("sample_task", {"round": 1})
        return batch, expected, first_model

    @staticmethod
    def replay(root, batch, model):
        resumed = DecisionRecorder(model, root)
        resumed.begin_session(research_id="offline-research")
        resumed.bind_batch(batch)
        return resumed.complete_json("sample_task", {"round": 1})

    def test_verified_replay_never_reinvokes_provider(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            batch, expected, original = self.create_call(root)
            next_model = CountingModel()
            self.assertEqual(self.replay(root, batch, next_model), expected)
            self.assertEqual((original.calls, next_model.calls), (1, 0))
            events = (root / "logs" / "model_log.jsonl").read_text(
                encoding="utf-8").splitlines()
            self.assertEqual(len(events), 2)

    def test_intent_is_durable_before_model_invocation(self):
        class VerifyIntent(CountingModel):
            def __init__(self, root):
                super().__init__()
                self.root = root

            def complete_json(self, task, context, *, thread_path=None):
                records = list((self.root / "logs" / "model_calls").glob("*.record.json"))
                self_test.assertEqual(len(records), 1)
                record = json.loads(records[0].read_text(encoding="utf-8"))
                self_test.assertEqual(record["status"], "in_flight")
                self_test.assertIsNone(record["response_sha256"])
                manifest = json.loads((self.root / "batch" / "model_call_manifest.json")
                                      .read_text(encoding="utf-8"))
                self_test.assertEqual(len(manifest["calls"]), 1)
                return super().complete_json(task, context, thread_path=thread_path)

        self_test = self
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            recorder = DecisionRecorder(VerifyIntent(root), root)
            recorder.begin_session(research_id="offline-research")
            recorder.bind_batch(root / "batch")
            self.assertEqual(recorder.complete_json("sample_task", {"round": 1}),
                             {"task": "sample_task", "round": 1})

    def test_damaged_manifest_rejects_replay_before_provider_call(self):
        for damage in ("nondict", "duplicate", "missing_id"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                batch, _, _ = self.create_call(root)
                path = batch / "model_call_manifest.json"
                manifest = json.loads(path.read_text(encoding="utf-8"))
                original = manifest["calls"][0]
                if damage == "nondict":
                    manifest["calls"] = ["broken"]
                elif damage == "duplicate":
                    manifest["calls"] = [original, copy.deepcopy(original)]
                else:
                    manifest["calls"] = [{**original, "call_id": None}]
                path.write_text(json.dumps(manifest), encoding="utf-8")
                model = CountingModel()
                recorder = DecisionRecorder(model, root)
                recorder.begin_session(research_id="offline-research")
                recorder.batch_dir = batch
                with self.assertRaisesRegex(ModelError, "invalid call entries"):
                    recorder.complete_json("sample_task", {"round": 1})
                self.assertEqual(model.calls, 0)

    def test_audit_identity_mismatch_blocks_replay(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            batch, _, _ = self.create_call(root)
            manifest = json.loads((batch / "model_call_manifest.json")
                                  .read_text(encoding="utf-8"))
            record_path = Path(manifest["calls"][0]["global_record_ref"])
            audit = json.loads(record_path.read_text(encoding="utf-8"))
            audit["task"] = "other_task"
            record_path.write_text(json.dumps(audit), encoding="utf-8")
            model = CountingModel()
            with self.assertRaisesRegex(ModelError, "audit identity mismatch"):
                self.replay(root, batch, model)
            self.assertEqual(model.calls, 0)

    def test_missing_response_blocks_even_for_mock(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            batch, _, _ = self.create_call(root)
            manifest = json.loads((batch / "model_call_manifest.json")
                                  .read_text(encoding="utf-8"))["calls"][0]
            Path(manifest["response_ref"]).unlink()
            Path(manifest["global_response_ref"]).unlink()
            model = CountingModel()
            with self.assertRaisesRegex(ModelError, "retry blocked"):
                self.replay(root, batch, model)
            self.assertEqual(model.calls, 0)

    def test_non_object_decision_is_persisted_as_an_error(self):
        class InvalidModel(CountingModel):
            def complete_json(self, task, context, *, thread_path=None):
                self.calls += 1
                return ["not a JSON object"]

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            model = InvalidModel()
            recorder = DecisionRecorder(model, root)
            recorder.begin_session(research_id="offline-research")
            recorder.bind_batch(root / "batch")
            with self.assertRaisesRegex(ModelError, "must be a JSON object"):
                recorder.complete_json("sample_task", {"round": 1})
            self.assertEqual(model.calls, 1)
            record, = list((root / "logs" / "model_calls").glob("*.record.json"))
            payload = json.loads(record.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "error")
            self.assertIsNotNone(payload["response_sha256"])

    def test_inconsistent_terminal_status_rejects_replay(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            batch, _, _ = self.create_call(root)
            manifest = json.loads((batch / "model_call_manifest.json")
                                  .read_text(encoding="utf-8"))
            record_path = Path(manifest["calls"][0]["global_record_ref"])
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["status"] = "error"
            record_path.write_text(json.dumps(record), encoding="utf-8")
            model = CountingModel()
            with self.assertRaisesRegex(ModelError, "status mismatch"):
                self.replay(root, batch, model)
            self.assertEqual(model.calls, 0)

    def test_in_flight_audit_can_reconcile_persisted_response_without_retry(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            batch, expected, _ = self.create_call(root)
            manifest = json.loads((batch / "model_call_manifest.json")
                                  .read_text(encoding="utf-8"))
            record_path = Path(manifest["calls"][0]["global_record_ref"])
            record = json.loads(record_path.read_text(encoding="utf-8"))
            record["status"] = "in_flight"
            record["response_sha256"] = None
            record_path.write_text(json.dumps(record), encoding="utf-8")
            model = CountingModel()
            self.assertEqual(self.replay(root, batch, model), expected)
            self.assertEqual(model.calls, 0)
            recovered = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(recovered["status"], "ok")
            self.assertTrue(recovered["response_sha256"])

    def test_batch_manifest_and_session_index_remain_stable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            batch, _, _ = self.create_call(root)
            manifest_path = batch / "model_call_manifest.json"
            before = manifest_path.read_bytes()
            recorder = DecisionRecorder(CountingModel(), root)
            recorder.begin_session(research_id="offline-research")
            recorder.bind_batch(batch)
            self.assertEqual(manifest_path.read_bytes(), before)
            manifest = json.loads(before)
            self.assertEqual(len(manifest["calls"]), 1)
            self.assertTrue(manifest["calls"][0]["response_sha256"])
            for row in manifest["calls"]:
                self.assertTrue(Path(row["context_ref"]).exists())
                self.assertTrue(Path(row["response_ref"]).exists())
                self.assertTrue(Path(row["record_ref"]).exists())


if __name__ == "__main__":
    unittest.main()
