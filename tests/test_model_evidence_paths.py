"""Adversarial offline model replay evidence-path trust boundary tests.

The manifest is never allowed to select which filesystem path is opened.
All fixtures are local, with no CLI, HTTP, SDK, or BRAIN calls.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_research.integrations.model import DecisionRecorder, ModelError


POLICY = {"max_calls": 5, "max_reported_tokens": 1000, "failover": "manual"}


class OfflineModel:
    config = {"provider": "mock"}
    last_usage = {"input_tokens": 5, "output_tokens": 3}
    last_model = "fixture"
    last_thread_id = None
    last_response_text = None

    def __init__(self):
        self.calls = 0

    def complete_json(self, task, context, *, thread_path=None):
        self.calls += 1
        return {"accepted": True, "index": context["index"]}


class ReplayEvidencePathsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.batch = self.root / "batch"
        self.first = OfflineModel()
        recorder = self.recorder(self.first)
        self.assertEqual(recorder.complete_json("task", {"index": 1}),
                         {"accepted": True, "index": 1})
        self.assertEqual(self.first.calls, 1)
        self.manifest_path = self.batch / "model_call_manifest.json"
        self.row = self.load_manifest()["calls"][0]

    def recorder(self, model=None):
        r = DecisionRecorder(model or OfflineModel(), self.root, governance=POLICY)
        r.begin_session(research_id="R1")
        r.bind_batch(self.batch)
        return r

    def load_manifest(self):
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def edit_row(self, **fields):
        manifest = self.load_manifest()
        manifest["calls"][0].update(fields)
        self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def replay_without_binding(self, model):
        # Binding may repair the manifest, which would hide adversarial input;
        # enter the provider-free replay path directly for each damage case.
        recorder = DecisionRecorder(model, self.root, governance=POLICY)
        recorder.begin_session(research_id="R1")
        recorder.batch_dir = self.batch
        return recorder.complete_json("task", {"index": 1})

    def test_every_manifest_evidence_path_is_pinned_before_opening(self):
        outside = self.root / "outside.json"
        outside.write_text('{"status": "ok"}', encoding="utf-8")
        for field in ("context_ref", "response_ref", "global_response_ref",
                      "record_ref", "global_record_ref"):
            with self.subTest(field=field):
                saved = self.manifest_path.read_bytes()
                try:
                    self.edit_row(**{field: str(outside)})
                    model = OfflineModel()
                    with self.assertRaisesRegex(ModelError, "trusted evidence path"):
                        self.replay_without_binding(model)
                    self.assertEqual(model.calls, 0)
                    self.assertEqual(self.first.calls, 1)
                finally:
                    self.manifest_path.write_bytes(saved)

    def test_relative_and_traversal_paths_rejected_even_if_file_exists(self):
        for forged in ("../outside.json", "model_calls/../outside.json",
                       str(self.root / "outside.json")):
            with self.subTest(path=forged):
                original = self.manifest_path.read_bytes()
                try:
                    self.edit_row(global_record_ref=forged)
                    model = OfflineModel()
                    with self.assertRaisesRegex(ModelError, "trusted evidence path"):
                        self.replay_without_binding(model)
                    self.assertEqual(model.calls, 0)
                finally:
                    self.manifest_path.write_bytes(original)

    def test_forged_call_id_cannot_select_another_global_record(self):
        model = OfflineModel()
        self.edit_row(call_id="../outside")
        with self.assertRaisesRegex(ModelError, "invalid call entries"):
            self.replay_without_binding(model)
        self.assertEqual(model.calls, 0)

    def test_swapped_call_id_with_same_context_is_not_readable(self):
        model = OfflineModel()
        self.edit_row(call_id="f" * 32)
        with self.assertRaisesRegex(ModelError, "trusted evidence path"):
            self.replay_without_binding(model)
        self.assertEqual(model.calls, 0)

    def test_symlinked_batch_response_cannot_escape_batch(self):
        target = self.root / "outside.json"
        target.write_text(Path(self.row["response_ref"]).read_text(encoding="utf-8"),
                          encoding="utf-8")
        copied = Path(self.row["response_ref"])
        copied.unlink()
        try:
            os.symlink(target, copied)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink unavailable on this runner: {exc}")
        model = OfflineModel()
        with self.assertRaisesRegex(ModelError, "trusted evidence path"):
            self.replay_without_binding(model)
        self.assertEqual(model.calls, 0)

    def test_symlinked_global_audit_record_is_rejected(self):
        outside = self.root / "outside-record.json"
        audit = Path(self.row["global_record_ref"])
        outside.write_bytes(audit.read_bytes())
        audit.unlink()
        try:
            os.symlink(outside, audit)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink unavailable on this runner: {exc}")
        model = OfflineModel()
        with self.assertRaisesRegex(ModelError, "trusted evidence path"):
            self.replay_without_binding(model)
        self.assertEqual(model.calls, 0)

    def test_symlinked_manifest_is_rejected_before_read(self):
        other = self.root / "outside-manifest.json"
        other.write_bytes(self.manifest_path.read_bytes())
        self.manifest_path.unlink()
        try:
            os.symlink(other, self.manifest_path)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink unavailable on this runner: {exc}")
        model = OfflineModel()
        with self.assertRaisesRegex(ModelError, "trusted evidence path"):
            self.replay_without_binding(model)
        self.assertEqual(model.calls, 0)

    def test_tampered_original_input_blocks_verified_budget_replay(self):
        record = Path(self.row["global_record_ref"])
        context_path = Path(self.row["global_response_ref"]).with_suffix(".json")
        context_path = (self.root / "logs" / "model_calls" /
                        (self.row["call_id"] + ".context.json"))
        context_path.write_text('{"index": 999}', encoding="utf-8")
        model = OfflineModel()
        with self.assertRaisesRegex(ModelError, "input checksum mismatch"):
            self.replay_without_binding(model)
        self.assertEqual(model.calls, 0)
        self.assertEqual(self.first.calls, 1)
        # Previously settled budget remains exactly one attempt. No replay
        # may silently amend its usage or issue a second reservation.
        self.assertEqual(self.recorder().budget.status("R1")["attempts"], 1)

    def test_bogus_audit_internal_paths_rejected(self):
        record_path = Path(self.row["global_record_ref"])
        record = json.loads(record_path.read_text(encoding="utf-8"))
        for key in ("context_ref", "response_ref"):
            with self.subTest(field=key):
                original = record_path.read_bytes()
                try:
                    changed = dict(record)
                    changed[key] = str(self.root / "other.json")
                    record_path.write_text(json.dumps(changed), encoding="utf-8")
                    model = OfflineModel()
                    with self.assertRaisesRegex(ModelError, "audit identity mismatch"):
                        self.replay_without_binding(model)
                    self.assertEqual(model.calls, 0)
                finally:
                    record_path.write_bytes(original)

    def test_audit_session_id_different_from_manifest_blocks_recovery(self):
        record_path = Path(self.row["global_record_ref"])
        record = json.loads(record_path.read_text(encoding="utf-8"))
        record["session_id"] = "a" * 32
        record_path.write_text(json.dumps(record), encoding="utf-8")
        model = OfflineModel()
        with self.assertRaisesRegex(ModelError, "audit identity mismatch"):
            self.replay_without_binding(model)
        self.assertEqual(model.calls, 0)

    def test_normal_verified_replay_returns_without_new_provider_call(self):
        model = OfflineModel()
        self.assertEqual(self.replay_without_binding(model),
                         {"accepted": True, "index": 1})
        self.assertEqual(model.calls, 0)


if __name__ == "__main__":
    unittest.main()
