"""Cross-recorder and cross-process model-call exclusivity tests.

Every backend is an in-memory offline fixture. No BRAIN or remote provider
is ever contacted. Both research and batch identities must remain durable.
"""
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from alpha_research.integrations.model import DecisionRecorder, ModelError
from alpha_research.state.writer_lock import SingleWriterLock


class LocalModel:
    config = {"provider": "mock"}

    def __init__(self, *, entered=None, release=None, abort=False):
        self.calls = 0
        self.entered = entered
        self.release = release
        self.abort = abort
        self.last_usage = {"input_tokens": 2, "output_tokens": 1}
        self.last_model = "offline-fixture"
        self.last_thread_id = None
        self.last_response_text = None

    def complete_json(self, task, context, *, thread_path=None):
        self.calls += 1
        if self.entered is not None:
            self.entered.set()
        if self.release is not None and not self.release.wait(8):
            raise AssertionError("test model was not released")
        if self.abort:
            raise KeyboardInterrupt("synthetic process termination")
        return {"task": task, "seq": context["seq"]}


def recorder(root, research_id, batch, model):
    item = DecisionRecorder(model, root)
    item.begin_session(research_id=research_id)
    item.bind_batch(batch)
    return item


class CrossInstanceModelTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_same_batch_contender_cannot_issue_duplicate_call(self):
        entered, release = threading.Event(), threading.Event()
        batch = self.root / "shared-batch"
        model1, model2 = LocalModel(entered=entered, release=release), LocalModel()
        first = recorder(self.root, "R1", batch, model1)
        other = recorder(self.root, "R1", batch, model2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(first.complete_json, "task", {"seq": 1})
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaisesRegex(ModelError, "another model call is active"):
                    other.complete_json("task", {"seq": 1})
                self.assertEqual(model2.calls, 0)
            finally:
                release.set()
            expected = pending.result(timeout=12)
        self.assertEqual(other.complete_json("task", {"seq": 1}), expected)
        self.assertEqual((model1.calls, model2.calls), (1, 0))
        manifest = json.loads((batch / "model_call_manifest.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["calls"]), 1)
        self.assertEqual(len(list((self.root / "logs" / "model_calls")
                                  .glob("*.record.json"))), 1)

    def test_same_research_id_across_different_batches_is_serialized(self):
        entered, release = threading.Event(), threading.Event()
        model1, model2 = LocalModel(entered=entered, release=release), LocalModel()
        first = recorder(self.root, "same-research", self.root / "batch-a", model1)
        other = recorder(self.root, "same-research", self.root / "batch-b", model2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(first.complete_json, "task", {"seq": 1})
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaisesRegex(ModelError, "another model call is active"):
                    other.complete_json("task", {"seq": 2})
                self.assertEqual(model2.calls, 0)
            finally:
                release.set()
            pending.result(timeout=12)
        self.assertEqual(other.complete_json("task", {"seq": 2}),
                         {"task": "task", "seq": 2})
        self.assertEqual((model1.calls, model2.calls), (1, 1))

    def test_different_research_ids_cannot_adopt_shared_batch_receipt(self):
        batch = self.root / "shared-batch"
        first_model, second_model = LocalModel(), LocalModel()
        first = recorder(self.root, "R1", batch, first_model)
        second = recorder(self.root, "R2", batch, second_model)
        first.complete_json("task", {"seq": 1})
        with self.assertRaisesRegex(ModelError, "audit identity mismatch"):
            second.complete_json("task", {"seq": 1})
        self.assertEqual((first_model.calls, second_model.calls), (1, 0))

    def test_two_independent_researches_and_batches_can_run_concurrently(self):
        barrier = threading.Barrier(2, timeout=8)

        class ParallelModel(LocalModel):
            def complete_json(self, task, context, *, thread_path=None):
                barrier.wait()
                return super().complete_json(task, context, thread_path=thread_path)

        a = recorder(self.root, "RA", self.root / "batch-a", ParallelModel())
        b = recorder(self.root, "RB", self.root / "batch-b", ParallelModel())
        with ThreadPoolExecutor(max_workers=2) as pool:
            fa = pool.submit(a.complete_json, "task", {"seq": 1})
            fb = pool.submit(b.complete_json, "task", {"seq": 2})
            self.assertEqual(fa.result(timeout=12)["seq"], 1)
            self.assertEqual(fb.result(timeout=12)["seq"], 2)

    def test_two_recorder_sessions_append_without_losing_batch_rows(self):
        batch = self.root / "shared-batch"
        first = recorder(self.root, "R1", batch, LocalModel())
        second = recorder(self.root, "R2", batch, LocalModel())
        self.assertEqual(first.complete_json("first", {"seq": 1})["seq"], 1)
        self.assertEqual(second.complete_json("second", {"seq": 2})["seq"], 2)
        manifest = json.loads((batch / "model_call_manifest.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual({r["task"] for r in manifest["calls"]},
                         {"first", "second"})
        self.assertEqual(len(set(r["call_id"] for r in manifest["calls"])), 2)
        self.assertEqual(len(manifest["session_ids"]), 2)
        for session_id in manifest["session_ids"]:
            session = json.loads((self.root / "logs" / "model_sessions" /
                                  f"{session_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(len(session["call_ids"]), 1)

    def test_batch_binding_fails_cleanly_while_call_is_in_flight(self):
        entered, release = threading.Event(), threading.Event()
        batch = self.root / "shared-batch"
        first = recorder(self.root, "R1", batch, LocalModel(
            entered=entered, release=release))
        second = DecisionRecorder(LocalModel(), self.root)
        second.begin_session(research_id="R2")
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(first.complete_json, "task", {"seq": 1})
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaisesRegex(ModelError, "another model call is active"):
                    second.bind_batch(batch)
                self.assertIsNone(second.batch_dir)
            finally:
                release.set()
            pending.result(timeout=12)

    def test_manifest_read_merge_write_lock_rejects_competing_writer(self):
        batch = self.root / "batch"
        item = recorder(self.root, "R1", batch, LocalModel())
        path = batch / "model_call_manifest.lock"
        before = (batch / "model_call_manifest.json").read_bytes()
        with SingleWriterLock(path):
            with self.assertRaisesRegex(ModelError, "another writer is updating"):
                item.journal.sync_batch_calls(
                    [], batch_dir=batch, session_id=item.session_id)
        self.assertEqual((batch / "model_call_manifest.json").read_bytes(), before)

    def test_interrupted_model_call_is_not_retried_by_another_recorder(self):
        batch = self.root / "batch"
        first_model = LocalModel(abort=True)
        first = recorder(self.root, "R1", batch, first_model)
        with self.assertRaisesRegex(KeyboardInterrupt, "synthetic process termination"):
            first.complete_json("task", {"seq": 1})
        model = LocalModel()
        recovered = recorder(self.root, "R1", batch, model)
        with self.assertRaisesRegex(ModelError, "retry blocked"):
            recovered.complete_json("task", {"seq": 1})
        self.assertEqual((first_model.calls, model.calls), (1, 0))
        record, = (self.root / "logs" / "model_calls").glob("*.record.json")
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))["status"],
                         "in_flight")

    def test_subprocess_recognizes_batch_lock_across_process_boundary(self):
        batch = self.root / "batch"
        recorder(self.root, "R1", batch, LocalModel())
        path = batch / "model_call.invoke.lock"
        code = (
            "import sys\n"
            "from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError\n"
            "try:\n"
            "    with SingleWriterLock(sys.argv[1]):\n"
            "        print('ACQUIRED')\n"
            "except WriterLockError:\n"
            "    print('BUSY')\n"
        )

        def check():
            return subprocess.run(
                [sys.executable, "-c", code, str(path)],
                cwd=Path(__file__).resolve().parent.parent,
                capture_output=True, text=True, timeout=15, check=True,
            ).stdout.strip()

        with SingleWriterLock(path):
            self.assertEqual(check(), "BUSY")
        self.assertEqual(check(), "ACQUIRED")


if __name__ == "__main__":
    unittest.main()
