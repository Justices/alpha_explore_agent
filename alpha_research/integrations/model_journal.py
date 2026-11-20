"""Durable model-call audit journal and incremental batch manifests.

This module never invokes a provider. Existing on-disk layout and event
ordering are deliberately preserved for recovery and forensic continuity.
"""
import json
import os
import shutil
from pathlib import Path

from alpha_research.integrations.model_errors import ModelError
from alpha_research.state.store import atomic_write_json
from alpha_research.state.writer_lock import SingleWriterLock


class ModelCallJournal:
    def __init__(self, state_root):
        self.logs_root = Path(state_root).resolve() / "logs"
        self.log_path = self.logs_root / "model_log.jsonl"

    def append_event(self, record):
        """Append one audit EVENT; .record.json remains the latest-state source.

        Unlike rewriting the full JSONL file for every status transition,
        append-only events have bounded work per model call and preserve both
        the in-flight and terminal transitions for forensics.
        """
        self.logs_root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        with SingleWriterLock(self.logs_root / "model_log.lock"):
            with self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())

    def sync_batch_calls(self, calls, *, batch_dir, session_id):
        """One source of truth for batch model-call manifests.

        Rebinding a completed call is idempotent. It does not recopy its
        context, response or audit record, even as the session grows.
        """
        if batch_dir is None:
            return
        target = batch_dir / "model_calls"
        target.mkdir(parents=True, exist_ok=True)
        manifest_path = batch_dir / "model_call_manifest.json"
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            previous = {}
        except (OSError, ValueError) as exc:
            raise ModelError("batch model call manifest is unreadable") from exc
        if not isinstance(previous, dict) or not isinstance(previous.get("calls", []), list):
            raise ModelError("batch model call manifest has invalid schema")
        rows = previous.get("calls", [])
        if (any(not isinstance(row, dict)
                or not isinstance(row.get("call_id"), str)
                or not row["call_id"] for row in rows)
                or len({row["call_id"] for row in rows}) != len(rows)):
            raise ModelError("batch model call manifest has invalid call entries")
        entries = {row["call_id"]: row for row in rows}
        changed = not manifest_path.exists()
        for record, context_path, response_path, record_path in calls:
            copied_context = target / context_path.name
            copied_response = target / response_path.name
            copied_record = target / record_path.name
            entry = {
                "call_id": record["call_id"], "task": record["task"],
                "session_id": record.get("session_id"),
                "context_sha256": record["context_sha256"],
                "route_sha256": record.get("route_sha256"),
                "response_sha256": record.get("response_sha256"),
                "context_ref": str(copied_context),
                "response_ref": str(copied_response),
                "global_response_ref": str(response_path),
                "record_ref": str(copied_record),
                "global_record_ref": str(record_path),
            }
            old = entries.get(record["call_id"])
            valid_copy = (old == entry and copied_context.is_file()
                          and copied_record.is_file()
                          and (not response_path.exists() or copied_response.is_file()))
            if valid_copy:
                continue
            if not copied_context.exists():
                shutil.copy2(context_path, copied_context)
            shutil.copy2(record_path, copied_record)
            if response_path.exists():
                shutil.copy2(response_path, copied_response)
            entries[record["call_id"]] = entry
            changed = True
        if changed:
            atomic_write_json(manifest_path, {
                "session_ids": sorted({row.get("session_id") for row in entries.values()
                                       if row.get("session_id")}),
                "calls": sorted(entries.values(), key=lambda row: row["call_id"]),
            })
        if session_id:
            sessions_root = self.logs_root / "model_sessions"
            sessions_root.mkdir(parents=True, exist_ok=True)
            index_path = sessions_root / f"{session_id}.json"
            session_index = {
                "session_id": session_id,
                "batch_ref": str(batch_dir),
                "call_ids": sorted(row["call_id"] for row in entries.values()
                                   if row.get("session_id") == session_id),
            }
            if (not index_path.exists() or
                    json.loads(index_path.read_text(encoding="utf-8")) != session_index):
                atomic_write_json(index_path, session_index)

