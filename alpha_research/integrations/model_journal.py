"""Durable model-call audit journal and incremental batch manifests.

This module never invokes a provider. Global call evidence paths and event
ordering remain stable; Session indices use the strict v2 contract.
"""
import json
import os
import shutil
import time
from uuid import uuid4
import hashlib
from contextlib import contextmanager
from pathlib import Path

from alpha_research.integrations.model_errors import ModelError
from alpha_research.integrations.model_evidence import (
    check_trusted_path, open_trusted_directory, open_trusted_evidence,
    read_trusted_json, trusted_replay_paths,
)
from alpha_research.integrations.model_sessions import index_path, merge_index
from alpha_research.state.fingerprint import canonical_json_bytes as _json_bytes
from alpha_research.state.store import atomic_write_json
from alpha_research.state.sealed import seal_state
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError


class ModelCallJournal:
    def __init__(self, state_root):
        self.logs_root = Path(state_root).resolve() / "logs"
        self.log_path = self.logs_root / "model_log.jsonl"
        self.calls_root = self.logs_root / "model_calls"
        self.session_calls = []

    def reset_session(self):
        """A fresh session owns its in-memory list, never old persisted calls."""
        self.session_calls = []

    def call_paths(self, call_id):
        return (self.calls_root / f"{call_id}.context.json",
                self.calls_root / f"{call_id}.response.json",
                self.calls_root / f"{call_id}.record.json")

    def persist_intent(self, record, context, *, batch_dir, session_id):
        """Write the call intent and batch references before the provider runs."""
        check_trusted_path(self.calls_root, label="global calls directory")
        self.calls_root.mkdir(parents=True, exist_ok=True)
        context_path, response_path, record_path = self.call_paths(record["call_id"])
        for path in (context_path, response_path, record_path):
            check_trusted_path(path, label="global model evidence")
        atomic_write_json(context_path, context)
        atomic_write_json(record_path, record)
        call = (record, context_path, response_path, record_path)
        self.sync_batch_calls(
            [call], batch_dir=batch_dir, session_id=session_id,
            research_id=record.get("research_id"))
        self.append_event(record)
        return context_path, response_path, record_path

    def persist_outcome(self, record, response, paths, *, batch_dir,
                        session_id, token_in, token_out, latency_ms,
                        actual_model=None, thread_id=None):
        """Persist response -> audit -> event -> manifest in the existing order.

        Crash-gap replay uses either the batch copy or the global response;
        an exception before completion leaves the original budget reservation
        unresolved and never authorizes a second provider request.
        """
        context_path, response_path, record_path = paths
        # The checksum and response must land in the *same* atomic JSON file.
        # A crash after writing this file but before terminal audit/manifest
        # writes still leaves verifiable evidence for provider-free replay.
        sealed_response = seal_state(dict(response))
        atomic_write_json(response_path, sealed_response)
        record.update({
            "response_sha256": hashlib.sha256(_json_bytes(sealed_response)).hexdigest(),
            "token_in": token_in, "token_out": token_out,
            "latency_ms": latency_ms, "status": response["status"],
        })
        if isinstance(actual_model, str) and actual_model:
            record["model"] = actual_model
        if isinstance(thread_id, str):
            record["codex_thread_id"] = thread_id
        atomic_write_json(record_path, record)
        self.append_event(record)
        call = (record, context_path, response_path, record_path)
        self.session_calls.append(call)
        self.sync_batch_calls(
            [call], batch_dir=batch_dir, session_id=session_id,
            research_id=record.get("research_id"))


    @staticmethod
    def _copy_batch_evidence(source, destination):
        """Expose only complete copies, never partially written audit files."""
        destination = Path(destination)
        # Leave room for deeply nested Dataset review evidence on Windows.
        temporary = destination.with_name(".tmp-" + uuid4().hex)
        # Pin the destination directory so a POSIX directory swap cannot
        # redirect either the temporary write or its final atomic rename.
        # Windows has no dir_fd/openat support: use verified paths there.
        with open_trusted_directory(
                destination.parent, label="batch evidence directory") as directory_fd:
            try:
                with open_trusted_evidence(
                        source, label="global model evidence") as src:
                    if directory_fd is None:
                        target = temporary.open("xb")
                    else:
                        fd = os.open(
                            temporary.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600, dir_fd=directory_fd)
                        target = os.fdopen(fd, "wb")
                    with target:
                        # No copy2 metadata round-trips; evidence content and
                        # fsync are the only required copy properties.
                        shutil.copyfileobj(src, target)
                        target.flush()
                        os.fsync(target.fileno())
                if directory_fd is None:
                    os.replace(temporary, destination)
                else:
                    os.replace(
                        temporary.name, destination.name,
                        src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            finally:
                if directory_fd is None:
                    temporary.unlink(missing_ok=True)
                else:
                    try:
                        os.unlink(temporary.name, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass

    def append_event(self, record):
        """Append one audit EVENT; .record.json remains the latest-state source.

        Unlike rewriting the full JSONL file for every status transition,
        append-only events have bounded work per model call and preserve both
        the in-flight and terminal transitions for forensics.
        """
        self.logs_root.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        # Different studies can reach this short append-only critical
        # section concurrently. Wait *only* for this local audit file lock;
        # never retry a provider invocation or a batch/research intent.
        deadline = time.monotonic() + 2.0
        while True:
            try:
                with SingleWriterLock(self.logs_root / "model_log.lock"):
                    with self.log_path.open("a", encoding="utf-8", newline="\n") as handle:
                        handle.write(line)
                        handle.flush()
                        os.fsync(handle.fileno())
                return
            except WriterLockError as exc:
                if time.monotonic() >= deadline:
                    raise ModelError("model audit event writer remained busy") from exc
                time.sleep(0.01)

    @contextmanager
    def _session_index_guard(self, session_id):
        """Acquire the per-session OS lock while holding the batch lock.

        Batch -> session is the only lock order. A bounded wait permits two
        independent batches of one session to merge without lost updates.
        """
        target = check_trusted_path(
            index_path(self.logs_root, session_id), label="session index")
        target.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 2.0
        while True:
            guard = SingleWriterLock(target.with_suffix(".lock"))
            try:
                guard.__enter__()
            except WriterLockError as exc:
                if time.monotonic() >= deadline:
                    raise ModelError("model session index writer remained busy") from exc
                time.sleep(0.01)
            else:
                break
        try:
            yield target
        finally:
            guard.__exit__(None, None, None)

    def sync_batch_calls(self, calls, *, batch_dir, session_id, research_id):
        """Atomically merge batch records and the strictly versioned session index."""
        if batch_dir is None:
            return
        if session_id is None or not isinstance(research_id, str) or not research_id.strip():
            raise ModelError("model session and research IDs are required for batch sync")
        batch_dir = Path(batch_dir).resolve()
        check_trusted_path(batch_dir / "model_call_manifest.json",
                           label="batch manifest")
        check_trusted_path(batch_dir / "model_calls",
                           label="batch evidence directory")
        # The lock spans read -> merge -> copies -> manifest/index write.
        # Provider calls never hold this *manifest* lock.
        try:
            with SingleWriterLock(batch_dir / "model_call_manifest.lock"):
                with self._session_index_guard(session_id) as session_path:
                    return self._sync_batch_calls_locked(
                        calls, batch_dir=batch_dir, session_id=session_id,
                        session_path=session_path, research_id=research_id)
        except WriterLockError as exc:
            raise ModelError(
                "another writer is updating this batch model manifest") from exc

    def _sync_batch_calls_locked(self, calls, *, batch_dir, session_id,
                                 session_path, research_id):
        """Rebinding completed calls is idempotent; never re-copy unchanged rows."""
        target = batch_dir / "model_calls"
        target.mkdir(parents=True, exist_ok=True)
        manifest_path = check_trusted_path(
            batch_dir / "model_call_manifest.json", label="batch manifest")
        try:
            previous = read_trusted_json(manifest_path, label="batch manifest")
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
        # Existing rows may be from another task or Session. Do not let
        # malformed references pass through a rebind and poison the Session
        # index even when the current task does not match that old row.
        for row in rows:
            trusted_replay_paths(batch_dir, self.calls_root, row)
        entries = {row["call_id"]: row for row in rows}
        changed = not manifest_path.exists()
        for record, context_path, response_path, record_path in calls:
            # Copy only artifacts generated under this state root.
            canonical_sources = self.call_paths(record["call_id"])
            for actual, expected in zip(
                    (context_path, response_path, record_path),
                    canonical_sources):
                if Path(actual) != expected:
                    raise ModelError("model evidence source is not canonical")
                check_trusted_path(actual, label="global model evidence")
            copied_context = check_trusted_path(
                target / context_path.name, label="batch context")
            copied_response = check_trusted_path(
                target / response_path.name, label="batch response")
            copied_record = check_trusted_path(
                target / record_path.name, label="batch audit")
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
                self._copy_batch_evidence(context_path, copied_context)
            self._copy_batch_evidence(record_path, copied_record)
            if response_path.exists():
                self._copy_batch_evidence(response_path, copied_response)
            entries[record["call_id"]] = entry
            changed = True
        if session_path is not None:
            try:
                previous_index = read_trusted_json(session_path, label="session index")
            except FileNotFoundError:
                previous_index = None
            except (OSError, ValueError) as exc:
                raise ModelError("model session index is unreadable") from exc
            merged_index = merge_index(
                previous_index, session_id, research_id, str(batch_dir),
                [row["call_id"] for row in entries.values()
                 if row.get("session_id") == session_id])
        if changed:
            atomic_write_json(manifest_path, {
                "session_ids": sorted({row.get("session_id") for row in entries.values()
                                       if row.get("session_id")}),
                "calls": sorted(entries.values(), key=lambda row: row["call_id"]),
            })
        if merged_index != previous_index:
            atomic_write_json(session_path, merged_index)
