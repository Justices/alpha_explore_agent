"""Offline multi-batch model-session index invariants (current contract only)."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

from alpha_research.integrations.model import DecisionRecorder, ModelError
from alpha_research.integrations.model_journal import ModelCallJournal
from alpha_research.integrations.model_sessions import merge_index


class CountingModel:
    config = {"provider": "mock"}
    last_usage = {"input_tokens": 2, "output_tokens": 1}
    last_model = "offline"
    last_thread_id = None
    last_response_text = None

    def __init__(self):
        self.calls = 0

    def complete_json(self, task, context, *, thread_path=None):
        self.calls += 1
        return {"task": task, "round": context["round"]}


class SessionIndexTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.session_id = uuid4().hex
        self.index_path = (self.root / "logs" / "model_sessions" /
                           (self.session_id + ".json"))

    def recorder(self, *, model=None, research_id="R1"):
        r = DecisionRecorder(model or CountingModel(), self.root)
        r.begin_session(self.session_id, research_id=research_id)
        return r

    def index(self):
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def test_batch_binding_before_explicit_session_start_keeps_index(self):
        model = CountingModel()
        recorder = DecisionRecorder(model, self.root)
        batch = self.root / "initial-batch"
        self.assertIsNone(recorder.session_id)
        recorder.bind_batch(batch)
        generated = recorder.session_id
        self.assertIsInstance(generated, str)
        self.assertEqual(len(generated), 32)
        self.assertEqual(recorder.batch_dir, batch.resolve())
        self.assertEqual(recorder.complete_json("first", {"round": 1}),
                         {"task": "first", "round": 1})
        self.assertEqual(recorder.session_id, generated)
        index_path = self.root / "logs" / "model_sessions" / (generated + ".json")
        index = json.loads(index_path.read_text(encoding="utf-8"))
        self.assertEqual(index["research_id"], generated)
        self.assertEqual(len(index["call_ids"]), 1)
        self.assertEqual(index["schema_version"], 2)
        self.assertEqual(index["batches"][0]["batch_ref"], str(batch.resolve()))
        self.assertEqual(model.calls, 1)

    def test_one_session_keeps_distinct_batch_memberships(self):
        r = self.recorder()
        a, b = self.root / "a", self.root / "b"
        r.bind_batch(a)
        r.complete_json("first", {"round": 1})
        a_before = json.loads((a / "model_call_manifest.json").read_text(encoding="utf-8"))
        first_call = a_before["calls"][0]["call_id"]
        r.bind_batch(b)
        # Older calls owned by A may not be silently copied into B.
        self.assertEqual(json.loads((b / "model_call_manifest.json")
                                    .read_text(encoding="utf-8"))["calls"], [])
        r.complete_json("second", {"round": 2})
        b_calls = json.loads((b / "model_call_manifest.json")
                             .read_text(encoding="utf-8"))["calls"]
        self.assertEqual(len(b_calls), 1)
        second_call = b_calls[0]["call_id"]
        self.assertNotEqual(first_call, second_call)
        index = self.index()
        self.assertEqual(index["session_id"], self.session_id)
        self.assertEqual(index["schema_version"], 2)
        self.assertEqual(index["research_id"], "R1")
        self.assertEqual(index["call_ids"], sorted([first_call, second_call]))
        self.assertEqual(index["batches"], [
            {"batch_ref": str(a.resolve()), "call_ids": [first_call]},
            {"batch_ref": str(b.resolve()), "call_ids": [second_call]},
        ])
        self.assertEqual(a_before, json.loads((a / "model_call_manifest.json")
                                             .read_text(encoding="utf-8")))

    def test_rebinding_old_batch_does_not_copy_later_call_or_rewrite_index(self):
        r = self.recorder()
        a, b = self.root / "a", self.root / "b"
        r.bind_batch(a)
        r.complete_json("first", {"round": 1})
        r.bind_batch(b)
        r.complete_json("second", {"round": 2})
        before = self.index_path.read_bytes()
        r.bind_batch(a)
        self.assertEqual(self.index_path.read_bytes(), before)
        manifest = json.loads((a / "model_call_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual([row["task"] for row in manifest["calls"]], ["first"])

    def test_unbound_session_history_still_attaches_to_first_batch(self):
        r = self.recorder()
        r.complete_json("before_batch", {"round": 0})
        batch = self.root / "a"
        r.bind_batch(batch)
        manifest = json.loads((batch / "model_call_manifest.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["calls"]), 1)
        self.assertEqual(manifest["calls"][0]["task"], "before_batch")
        self.assertEqual(self.index()["call_ids"], [manifest["calls"][0]["call_id"]])

    def test_unbound_call_is_attached_only_to_its_first_batch(self):
        r = self.recorder()
        r.complete_json("before_batch", {"round": 0})
        a, b = self.root / "a", self.root / "b"
        r.bind_batch(a)
        first = json.loads((a / "model_call_manifest.json")
                           .read_text(encoding="utf-8"))["calls"]
        self.assertEqual(len(first), 1)
        r.bind_batch(b)
        second = json.loads((b / "model_call_manifest.json")
                            .read_text(encoding="utf-8"))["calls"]
        self.assertEqual(second, [])
        self.assertEqual(self.index()["batches"], [
            {"batch_ref": str(a.resolve()), "call_ids": [first[0]["call_id"]]},
            {"batch_ref": str(b.resolve()), "call_ids": []},
        ])

    def test_v2_rejects_missing_and_extra_contract_fields(self):
        r = self.recorder()
        r.bind_batch(self.root / "a")
        previous = self.index()
        for corrupt in (
            {k: v for k, v in previous.items() if k != "research_id"},
            {**previous, "batch_ref": str(self.root / "a")},
            {**previous, "schema_version": 1},
        ):
            with self.subTest(corrupt=corrupt):
                self.index_path.write_text(json.dumps(corrupt), encoding="utf-8")
                with self.assertRaisesRegex(ModelError, "current contract"):
                    r.bind_batch(self.root / "b")
        self.index_path.write_text(json.dumps(previous), encoding="utf-8")

    def test_previous_three_field_index_is_rejected_not_migrated(self):
        r = self.recorder()
        r.bind_batch(self.root / "a")
        before = self.index()
        prior = {key: before[key] for key in ("session_id", "call_ids")}
        prior["batch_ref"] = str(self.root / "a")
        self.index_path.write_text(json.dumps(prior), encoding="utf-8")
        with self.assertRaisesRegex(ModelError, "current contract"):
            r.bind_batch(self.root / "b")
        self.assertEqual(self.index(), prior)

    def test_independent_recorders_merge_same_session_without_lost_update(self):
        a, b = self.root / "a", self.root / "b"
        barrier = Barrier(2, timeout=6)

        def bind(path):
            r = self.recorder()
            barrier.wait()
            r.bind_batch(path)
            return r.session_id

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(bind, path) for path in (a, b)]
            self.assertEqual([f.result(timeout=12) for f in futures],
                             [self.session_id, self.session_id])
        self.assertEqual(self.index()["batches"], [
            {"batch_ref": str(a.resolve()), "call_ids": []},
            {"batch_ref": str(b.resolve()), "call_ids": []},
        ])

    def test_index_write_crash_can_repair_from_manifest_without_provider(self):
        r = self.recorder()
        batch = self.root / "a"
        r.bind_batch(batch)
        model = r.model
        from alpha_research.integrations import model_journal
        real_write = model_journal.atomic_write_json

        def fail_index(path, content):
            if Path(path) == self.index_path:
                raise RuntimeError("injected session index write crash")
            return real_write(path, content)

        with patch.object(model_journal, "atomic_write_json", side_effect=fail_index):
            with self.assertRaisesRegex(RuntimeError, "index write crash"):
                r.complete_json("first", {"round": 1})
        self.assertEqual(model.calls, 0)
        manifest = json.loads((batch / "model_call_manifest.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["calls"]), 1)
        self.assertIsNone(manifest["calls"][0]["response_sha256"])
        self.assertEqual(self.index()["call_ids"], [])

        restarted = self.recorder()
        restarted.bind_batch(batch)  # repair only, never invoke provider
        self.assertEqual(restarted.model.calls, 0)
        self.assertEqual(self.index()["call_ids"], [manifest["calls"][0]["call_id"]])
        with self.assertRaisesRegex(ModelError, "retry blocked"):
            restarted.complete_json("first", {"round": 1})
        self.assertEqual(restarted.model.calls, 0)

    def test_corrupt_session_index_blocks_before_model_call(self):
        r = self.recorder()
        r.bind_batch(self.root / "a")
        self.index_path.write_text(
            json.dumps({"schema_version": 2, "session_id": self.session_id,
                        "research_id": "R1", "call_ids": ["a" * 32],
                        "batches": [{"batch_ref": str((self.root / "a").resolve()),
                                     "call_ids": []}]}), encoding="utf-8")
        next_model = CountingModel()
        contender = self.recorder(model=next_model)
        with self.assertRaisesRegex(ModelError, "conflicts with batch membership"):
            contender.bind_batch(self.root / "b")
        self.assertEqual(next_model.calls, 0)
        self.assertIsNone(contender.batch_dir)

    def test_explicit_session_id_cannot_escape_session_directory(self):
        r = DecisionRecorder(CountingModel(), self.root)
        for invalid in ("../outside", "invalid", "", "A" * 32,
                        "a" * 31, 13, "a" * 32 + "/extra"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ModelError, "session ID"):
                    r.begin_session(invalid, research_id="R1")
                self.assertIsNone(r.session_id)
        self.assertFalse((self.root / "logs" / "outside.json").exists())

    def test_session_id_cannot_be_reused_by_a_different_research(self):
        first = self.recorder(research_id="R1")
        first.bind_batch(self.root / "a")
        first.complete_json("first", {"round": 1})
        before = self.index_path.read_bytes()
        second_model = CountingModel()
        contender = self.recorder(model=second_model, research_id="R2")
        with self.assertRaisesRegex(ModelError, "different research"):
            contender.bind_batch(self.root / "b")
        self.assertIsNone(contender.batch_dir)
        self.assertEqual(second_model.calls, 0)
        self.assertEqual(self.index_path.read_bytes(), before)
        self.assertEqual(self.index()["research_id"], "R1")

    def test_missing_current_contract_fields_fail_closed(self):
        first = self.recorder(research_id="R1")
        first.bind_batch(self.root / "a")
        good = self.index()
        for missing in ("schema_version", "research_id", "batches", "call_ids"):
            with self.subTest(missing=missing):
                damaged = {k: v for k, v in good.items() if k != missing}
                self.index_path.write_text(json.dumps(damaged), encoding="utf-8")
                with self.assertRaisesRegex(ModelError, "current contract"):
                    first.bind_batch(self.root / "b")
        self.index_path.write_text(json.dumps(good), encoding="utf-8")

    def test_unrecognized_index_version_rejected(self):
        r = self.recorder()
        r.bind_batch(self.root / "a")
        before = self.index()
        before["schema_version"] = 1
        self.index_path.write_text(json.dumps(before), encoding="utf-8")
        with self.assertRaisesRegex(ModelError, "current contract"):
            r.bind_batch(self.root / "b")

    def test_current_index_rejects_unsafe_call_id(self):
        first = self.recorder()
        first.bind_batch(self.root / "a")
        corrupted = self.index()
        corrupted["call_ids"] = ["../../outside"]
        self.index_path.write_text(json.dumps(corrupted), encoding="utf-8")
        with self.assertRaisesRegex(ModelError, "invalid call IDs"):
            first.bind_batch(self.root / "b")

    def test_cross_batch_duplicate_call_is_rejected(self):
        left, right = "a" * 32, "b" * 32
        existing = {
            "schema_version": 2, "session_id": self.session_id,
            "research_id": "R1", "call_ids": [left],
            "batches": [{"batch_ref": "/a", "call_ids": [left]}],
        }
        with self.assertRaisesRegex(ModelError, "another batch"):
            merge_index(existing, self.session_id, "R1", "/b", [left, right])

    def test_persisted_duplicate_call_owner_is_rejected_even_if_union_matches(self):
        shared = "a" * 32
        corrupt = {
            "schema_version": 2, "session_id": self.session_id,
            "research_id": "R1", "call_ids": [shared],
            "batches": [
                {"batch_ref": "/a", "call_ids": [shared]},
                {"batch_ref": "/b", "call_ids": [shared]},
            ],
        }
        with self.assertRaisesRegex(ModelError, "belongs to another batch"):
            merge_index(corrupt, self.session_id, "R1", "/c", [])

    def test_pure_index_merge_rejects_conflicting_aggregate(self):
        index = {
            "schema_version": 2, "session_id": self.session_id,
            "research_id": "R1", "call_ids": ["a" * 32, "b" * 32],
            "batches": [{"batch_ref": "/a", "call_ids": ["a" * 32]}],
        }
        with self.assertRaisesRegex(ModelError, "conflicts with batch membership"):
            merge_index(index, self.session_id, "R1", "/b", [])


if __name__ == "__main__":
    unittest.main()
