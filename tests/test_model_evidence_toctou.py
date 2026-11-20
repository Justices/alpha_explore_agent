"""Deterministic offline swap races at the check/open boundary.

POSIX must bind each evidence read to a no-follow descriptor walk.
Windows checks pre/post-open identity and rejects observable reparse swaps.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_research.integrations.model import DecisionRecorder, ModelError
from alpha_research.integrations.model_evidence import (
    open_trusted_evidence, read_trusted_json,
)
from alpha_research.integrations import model_replay


class OfflineBackend:
    config = {"provider": "mock"}
    last_usage = {"input_tokens": 2, "output_tokens": 1}
    last_model = "fixture"
    last_thread_id = None
    last_response_text = None

    def __init__(self):
        self.calls = 0

    def complete_json(self, task, context, *, thread_path=None):
        self.calls += 1
        return {"task": task, "value": context["value"]}


class EvidenceSwapTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.batch = self.root / "batch"
        self.original = OfflineBackend()
        recorder = DecisionRecorder(self.original, self.root)
        recorder.begin_session(research_id="R1")
        recorder.bind_batch(self.batch)
        self.expected = recorder.complete_json("task", {"value": 7})
        manifest = json.loads(
            (self.batch / "model_call_manifest.json").read_text(encoding="utf-8"))
        self.row = manifest["calls"][0]

    def replay(self, backend):
        recorder = DecisionRecorder(backend, self.root)
        recorder.begin_session(research_id="R1")
        recorder.batch_dir = self.batch
        return recorder.complete_json("task", {"value": 7})

    def swap_to_link(self, path, data):
        outside = self.root / ("outside-" + path.name)
        outside.write_text(json.dumps(data), encoding="utf-8")
        backup = self.root / ("backup-" + path.name)
        path.rename(backup)
        try:
            os.symlink(outside, path)
        except (OSError, NotImplementedError) as exc:
            backup.rename(path)
            self.skipTest(f"symlinks are not available here: {exc}")

    def test_global_audit_replaced_after_path_validation_fails_closed(self):
        record_path = Path(self.row["global_record_ref"])
        wrapped = model_replay.trusted_replay_paths
        changed = []

        def swap_after_validation(*args, **kwargs):
            paths = wrapped(*args, **kwargs)
            if not changed:
                self.swap_to_link(record_path, {"status": "ok"})
                changed.append(True)
            return paths

        backend = OfflineBackend()
        with patch.object(model_replay, "trusted_replay_paths",
                          side_effect=swap_after_validation):
            with self.assertRaisesRegex(ModelError, "trusted evidence path"):
                self.replay(backend)
        self.assertEqual(backend.calls, 0)

    def test_original_context_replaced_immediately_before_open_fails_closed(self):
        context_path = (self.root / "logs" / "model_calls" /
                        (self.row["call_id"] + ".context.json"))
        original = model_replay.read_trusted_json
        changed = []

        def swap_at_read(path, *, label):
            if label == "global context" and not changed:
                self.swap_to_link(context_path, {"value": 7})
                changed.append(True)
            return original(path, label=label)

        backend = OfflineBackend()
        with patch.object(model_replay, "read_trusted_json",
                          side_effect=swap_at_read):
            with self.assertRaisesRegex(ModelError, "trusted evidence path"):
                self.replay(backend)
        self.assertEqual(backend.calls, 0)

    @unittest.skipUnless(os.name == "posix", "POSIX descriptor pinning")
    def test_opened_descriptor_cannot_be_redirected_by_path_replacement(self):
        evidence = self.root / "target.json"
        evidence.write_text('{"source":"trusted"}', encoding="utf-8")
        with open_trusted_evidence(evidence, label="fixture") as handle:
            evidence.rename(self.root / "original.json")
            evidence.write_text('{"source":"forged"}', encoding="utf-8")
            self.assertEqual(json.load(handle), {"source": "trusted"})
        self.assertEqual(
            read_trusted_json(evidence, label="fixture"),
            {"source": "forged"})

    def test_no_race_keeps_verified_replay_provider_free(self):
        backend = OfflineBackend()
        self.assertEqual(self.replay(backend), self.expected)
        self.assertEqual(backend.calls, 0)


if __name__ == "__main__":
    unittest.main()
