"""Append-only, hash-chained execution events for one orchestration Task.

Domain records remain authoritative for business state. This journal links their
lifecycle and evidence references into an ordered, tamper-evident execution trace.
No credentials, prompts, raw provider responses or expression bodies belong here.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError


SCHEMA_VERSION = 1


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha256(value):
    return hashlib.sha256(value).hexdigest()


class ExecutionEventJournalError(ValueError):
    pass


class ExecutionEventJournal:
    """Per-Task JSONL journal with monotonic sequence and SHA-256 hash chain."""

    def __init__(self, state_root, task_id):
        if (not isinstance(task_id, str) or len(task_id) != 32
                or any(ch not in "0123456789abcdef" for ch in task_id)):
            raise ExecutionEventJournalError("invalid Task ID")
        self.root = Path(state_root).resolve() / "state" / "logs" / "execution_events"
        self.path = self.root / f"{task_id}.jsonl"
        self.lock_path = self.path.with_suffix(".lock")
        self.task_id = task_id

    def _read_verified(self):
        if not self.path.exists():
            return []
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ExecutionEventJournalError("execution event journal is unreadable") from exc
        rows = []
        previous = None
        for number, line in enumerate(raw.splitlines(), start=1):
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise ExecutionEventJournalError(
                    f"execution event journal has invalid JSON at line {number}") from exc
            if not isinstance(row, dict):
                raise ExecutionEventJournalError("execution event must be an object")
            supplied = row.get("event_sha256")
            body = {key: value for key, value in row.items() if key != "event_sha256"}
            if (row.get("schema_version") != SCHEMA_VERSION
                    or row.get("task_id") != self.task_id
                    or row.get("sequence") != number
                    or row.get("previous_event_sha256") != previous
                    or supplied != _sha256(_canonical(body))):
                raise ExecutionEventJournalError(
                    f"execution event hash chain mismatch at line {number}")
            rows.append(row)
            previous = supplied
        return rows

    def append(self, *, workflow, event_type, research_id=None, step=None,
               payload=None):
        if not isinstance(workflow, str) or not workflow.strip():
            raise ExecutionEventJournalError("workflow is required")
        if not isinstance(event_type, str) or not event_type.strip():
            raise ExecutionEventJournalError("event type is required")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ExecutionEventJournalError("event payload must be an object")
        try:
            _canonical(payload)
        except (TypeError, ValueError) as exc:
            raise ExecutionEventJournalError("event payload must be finite JSON") from exc
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            with SingleWriterLock(self.lock_path):
                rows = self._read_verified()
                body = {
                    "schema_version": SCHEMA_VERSION,
                    "sequence": len(rows) + 1,
                    "event_id": uuid4().hex,
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "task_id": self.task_id,
                    "workflow": workflow,
                    "research_id": research_id,
                    "step": step,
                    "event_type": event_type,
                    "payload": payload,
                    "previous_event_sha256": (
                        rows[-1]["event_sha256"] if rows else None),
                }
                row = {**body, "event_sha256": _sha256(_canonical(body))}
                with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(_canonical(row).decode("utf-8") + "\n")
                    handle.flush()
                    import os
                    os.fsync(handle.fileno())
                return row
        except WriterLockError as exc:
            raise ExecutionEventJournalError(
                "another writer is appending execution events") from exc

    def read_events(self):
        """Return all events only after verifying the complete hash chain."""
        try:
            with SingleWriterLock(self.lock_path):
                return self._read_verified()
        except WriterLockError as exc:
            raise ExecutionEventJournalError(
                "execution event journal is being updated") from exc

    def verify(self):
        """Return a compact verification receipt without mutating the journal."""
        try:
            with SingleWriterLock(self.lock_path):
                rows = self._read_verified()
        except WriterLockError as exc:
            raise ExecutionEventJournalError(
                "execution event journal is being updated") from exc
        return {
            "status": "VERIFIED",
            "task_id": self.task_id,
            "event_count": len(rows),
            "first_event_sha256": rows[0]["event_sha256"] if rows else None,
            "last_event_sha256": rows[-1]["event_sha256"] if rows else None,
            "journal_ref": str(self.path),
        }
