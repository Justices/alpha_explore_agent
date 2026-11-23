"""Fail-closed Task and recovery JSON parsing, no external calls."""
import tempfile
import unittest
from pathlib import Path

from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.recovery import RecoveryCoordinator, RecoveryError
from alpha_research.state.json_contract import read_strict_object
from alpha_research.state.store import atomic_write_json


class StrictStateRecordTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.TemporaryDirectory()
        self.addCleanup(handle.cleanup)
        self.root = Path(handle.name)
        self.orch = MainOrchestrator(self.root)
        self.task_id = self.orch.create(
            "super", {"objective": "offline strict state check"})["task_id"]
        self.record = self.orch._path(self.task_id)

    def test_duplicate_and_nonfinite_record_never_admitted(self):
        for content in (
            '{"task_id":"x","task_id":"y"}',
            '{"schema_version": NaN}',
            '[1,2,3]',
            'null',
        ):
            with self.subTest(content=content):
                self.record.write_text(content, encoding="utf-8")
                with self.assertRaises(OrchestrationError):
                    self.orch.load(self.task_id)

    def test_boolean_schema_version_cannot_equal_integer_schema(self):
        import json
        data = json.loads(self.record.read_text(encoding="utf-8"))
        data["schema_version"] = True
        self.record.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(OrchestrationError, "identity/schema"):
            self.orch.load(self.task_id)

    def test_linked_task_cannot_read_outside_audit_root(self):
        secret = self.root / "private.json"
        secret.write_text('{"secret":"not-an-audit"}', encoding="utf-8")
        self.record.unlink()
        try:
            self.record.symlink_to(secret)
        except (OSError, NotImplementedError):
            self.skipTest("symlink unavailable")
        with self.assertRaisesRegex(OrchestrationError, "symlink"):
            self.orch.load(self.task_id)

    def test_recovery_journal_uses_the_same_strict_contract(self):
        path = self.root / "journal.json"
        path.write_text('{"status":"COMPLETED","status":"RUNNING"}', encoding="utf-8")
        with self.assertRaises(RecoveryError):
            RecoveryCoordinator._read_json(path, "recovery journal")

    def test_atomic_writer_never_serializes_nonfinite_json(self):
        path = self.root / "not-allowed.json"
        with self.assertRaises(ValueError):
            atomic_write_json(path, {"cost_usd_micros": float("nan")})
        self.assertFalse(path.exists())

    def test_strict_reader_rejects_non_objects(self):
        location = self.root / "blob.json"
        location.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must contain a JSON object"):
            read_strict_object(location, "domain record")


if __name__ == "__main__":
    unittest.main()
