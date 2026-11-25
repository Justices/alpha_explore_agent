"""Integrity and lifecycle tests for append-only execution event journals."""
import json
import tempfile
import unittest

from alpha_research.orchestration.event_journal import (
    ExecutionEventJournal, ExecutionEventJournalError,
)


class ExecutionEventJournalTests(unittest.TestCase):
    def test_append_and_verify_monotonic_hash_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = ExecutionEventJournal(directory, "a" * 32)
            first = journal.append(
                workflow="regular", event_type="task.created",
                payload={"request_sha256": "abc"})
            second = journal.append(
                workflow="regular", event_type="task.step_started",
                step="execute", payload={"budget": {"limit": 1}})
            self.assertEqual(first["sequence"], 1)
            self.assertIsNone(first["previous_event_sha256"])
            self.assertEqual(second["sequence"], 2)
            self.assertEqual(second["previous_event_sha256"], first["event_sha256"])
            receipt = journal.verify()
            self.assertEqual(receipt["status"], "VERIFIED")
            self.assertEqual(receipt["event_count"], 2)
            self.assertEqual(
                [row["event_type"] for row in journal.read_events()],
                ["task.created", "task.step_started"],
            )

    def test_missing_journal_is_not_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = ExecutionEventJournal(directory, "c" * 32)
            receipt = journal.verify()
            self.assertEqual(receipt["status"], "MISSING")
            self.assertEqual(receipt["event_count"], 0)

    def test_empty_existing_journal_is_not_reinitialized(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = ExecutionEventJournal(directory, "d" * 32)
            journal.root.mkdir(parents=True)
            journal.path.write_text("", encoding="utf-8")
            with self.assertRaises(ExecutionEventJournalError):
                journal.append(workflow="regular", event_type="task.created")

    def test_tampered_event_is_detected_and_append_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = ExecutionEventJournal(directory, "b" * 32)
            journal.append(workflow="super", event_type="task.created")
            rows = journal.path.read_text(encoding="utf-8").splitlines()
            event = json.loads(rows[0])
            event["payload"]["forged"] = True
            journal.path.write_text(
                json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
            with self.assertRaises(ExecutionEventJournalError):
                journal.verify()
            with self.assertRaises(ExecutionEventJournalError):
                journal.append(workflow="super", event_type="task.step_started")


if __name__ == "__main__":
    unittest.main()
