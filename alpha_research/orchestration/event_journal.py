"""Append-only, hash-chained execution events for one orchestration Task.

Domain records remain authoritative for business state. This journal links their
lifecycle and evidence references into an ordered, tamper-evident execution trace.
No credentials, prompts, raw provider responses or expression bodies belong here.
"""
import hashlib
import json
import re
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
        if not raw or not raw.endswith("\n"):
            raise ExecutionEventJournalError(
                "execution event journal is empty or has an incomplete trailing record")
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
            if (type(row.get("schema_version")) is not int
                    or row.get("schema_version") != SCHEMA_VERSION
                    or row.get("task_id") != self.task_id
                    or type(row.get("sequence")) is not int
                    or row.get("sequence") != number
                    or not isinstance(row.get("event_id"), str)
                    or re.fullmatch(r"[0-9a-f]{32}", row["event_id"]) is None
                    or not isinstance(row.get("workflow"), str) or not row["workflow"]
                    or not isinstance(row.get("event_type"), str) or not row["event_type"]
                    or not isinstance(row.get("occurred_at"), str)
                    or not isinstance(row.get("payload"), dict)
                    or (row.get("research_id") is not None
                        and not isinstance(row.get("research_id"), str))
                    or (row.get("step") is not None and not isinstance(row.get("step"), str))
                    or row.get("previous_event_sha256") != previous
                    or not isinstance(supplied, str)
                    or re.fullmatch(r"[0-9a-f]{64}", supplied) is None
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
        if research_id is not None and not isinstance(research_id, str):
            raise ExecutionEventJournalError("Research ID must be a string")
        if step is not None and not isinstance(step, str):
            raise ExecutionEventJournalError("Task step must be a string")
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
        """Return all events only after verifying the complete hash chain.

        Reads intentionally do not acquire the writer lock: a trace inspection
        must not create lock files or mutate the State Root. A concurrent append
        that is observed mid-write is rejected by the JSONL/hash-chain checks.
        """
        return self._read_verified()

    def verify(self):
        """Return a compact verification receipt without mutating the journal."""
        rows = self._read_verified()
        if not self.path.is_file():
            return {
                "status": "MISSING", "task_id": self.task_id,
                "event_count": 0, "first_event_sha256": None,
                "last_event_sha256": None, "journal_ref": str(self.path),
            }
        return {
            "status": "VERIFIED",
            "task_id": self.task_id,
            "event_count": len(rows),
            "first_event_sha256": rows[0]["event_sha256"] if rows else None,
            "last_event_sha256": rows[-1]["event_sha256"] if rows else None,
            "journal_ref": str(self.path),
        }
