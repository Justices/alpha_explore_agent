"""Durable one-shot *non-executable* receipt reservation.

This records that a signed execution receipt has been inspected once. It
NEVER grants permission to dispatch a model/Gateway operation. A separate
trusted issuer, explicit user consent and production admission consumer are
still required. The primary by-ID ledger is authoritative across crashes;
missing derived projections are not automatically repaired/replayed.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.execution_authorization import (
    inspect_execution_receipt,
)
from alpha_research.state.store import atomic_write_json
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError

SCHEMA_VERSION = 1


def _root(orchestrator):
    return (Path(orchestrator.state_root) / "state" / "research_v1_1"
            / "execution_receipt_reservations")


def _load_ledger(path):
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "by_id": {}, "by_task": {}}
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OrchestrationError("execution receipt ledger unreadable") from exc
    if (not isinstance(ledger, dict)
            or set(ledger) != {"schema_version", "by_id", "by_task"}
            or ledger["schema_version"] != SCHEMA_VERSION
            or type(ledger["schema_version"]) is not int
            or not isinstance(ledger["by_id"], dict)
            or not isinstance(ledger["by_task"], dict)):
        raise OrchestrationError("execution receipt ledger schema invalid")
    # Both indexes are authoritative; a corrupted/unmatched mapping must
    # fail closed even when it concerns a DIFFERENT task/receipt ID.
    by_id, by_task = ledger["by_id"], ledger["by_task"]
    if (len(by_id) != len(by_task)
            or any(not isinstance(aid, str) or len(aid) != 32
                   or any(c not in "0123456789abcdef" for c in aid)
                   or not isinstance(tid, str) or len(tid) != 32
                   or any(c not in "0123456789abcdef" for c in tid)
                   or by_task.get(tid) != aid
                   for aid, tid in by_id.items())
            or any(by_id.get(aid) != tid for tid, aid in by_task.items())):
        raise OrchestrationError(
            "execution receipt ledger reciprocal index mismatch")
    return ledger


def reserve_non_executable_receipt(orchestrator, task_id, model_config,
                                   governance_policy, receipt, *,
                                   environ=None, now=None):
    """One-time record only; never executable and never reusable."""
    from alpha_research.orchestration.mock_workspace import marker_path
    if (marker_path(orchestrator.state_root).exists()
            or marker_path(orchestrator.state_root).is_symlink()):
        raise OrchestrationError("mock workspace cannot reserve execution receipts")
    checked = inspect_execution_receipt(
        orchestrator, task_id, model_config, governance_policy,
        receipt, environ=environ, now=now)
    root = _root(orchestrator)
    ledger_path = root / "reservations.json"
    item_path = root / (checked["authorization_id"] + ".json")
    task_path = root / "tasks" / (task_id + ".json")
    try:
        with SingleWriterLock(root / "reservations.lock"):
            # Re-verify the signed receipt and task state under the lock.
            checked = inspect_execution_receipt(
                orchestrator, task_id, model_config, governance_policy,
                receipt, environ=environ, now=now)
            ledger = _load_ledger(ledger_path)
            if (checked["authorization_id"] in ledger["by_id"]
                    or task_id in ledger["by_task"]
                    or item_path.exists() or task_path.exists()):
                raise OrchestrationError(
                    "execution receipt or task was already reserved; no replay")
            record = {
                "schema_version": SCHEMA_VERSION,
                "task_id": task_id,
                "authorization_id": checked["authorization_id"],
                "operation": checked["operation"],
                "receipt_sha256": checked["receipt_sha256"],
                "status": "RESERVED_INSPECTION_ONLY",
                "model_call_authorized": False,
                "brain_simulation_authorized": False,
                "alpha_submission_authorized": False,
                "permission_to_dispatch": False,
                "reserved_at": datetime.now(timezone.utc).isoformat(),
            }
            # The canonical single-file ledger is written first. A crash
            # after that write cannot re-use the ID or task.
            ledger["by_id"][checked["authorization_id"]] = task_id
            ledger["by_task"][task_id] = checked["authorization_id"]
            atomic_write_json(ledger_path, ledger)
            atomic_write_json(item_path, record)
            atomic_write_json(task_path, record)
            return record
    except WriterLockError as exc:
        raise OrchestrationError("execution receipt reservation lock busy") from exc


def inspect_non_executable_reservation(orchestrator, task_id):
    """Read-only status, never an authorization decision."""
    orchestrator._path(task_id)
    root = _root(orchestrator)
    ledger_path = root / "reservations.json"
    if not ledger_path.exists():
        # If the canonical ledger was lost but any derived projection exists,
        # no reservation can be silently considered new.
        if ((root / "tasks" / (task_id + ".json")).exists()
                or (root.exists() and any(root.glob("[0-9a-f]" * 32 + ".json")))):
            return {"task_id": task_id, "status": "RECONCILIATION_REQUIRED",
                    "permission_to_dispatch": False}
        return {"task_id": task_id, "status": "NOT_RESERVED",
                "permission_to_dispatch": False}
    try:
        ledger = _load_ledger(ledger_path)
        authorization_id = ledger["by_task"].get(task_id)
        if authorization_id is None:
            return {"task_id": task_id, "status": "NOT_RESERVED",
                    "permission_to_dispatch": False}
        if (not isinstance(authorization_id, str)
                or ledger["by_id"].get(authorization_id) != task_id):
            raise OrchestrationError("execution reservation reciprocal mapping invalid")
        item_path = root / (authorization_id + ".json")
        task_path = root / "tasks" / (task_id + ".json")
        record = json.loads(item_path.read_text(encoding="utf-8"))
        task_record = json.loads(task_path.read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or record != task_record
                or record.get("status") != "RESERVED_INSPECTION_ONLY"
                or record.get("authorization_id") != authorization_id
                or record.get("task_id") != task_id
                or record.get("permission_to_dispatch") is not False):
            raise OrchestrationError("execution reservation projections invalid")
        return {"task_id": task_id,
                "authorization_id": authorization_id,
                "status": "RESERVED_INSPECTION_ONLY",
                "permission_to_dispatch": False}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"task_id": task_id, "status": "RECONCILIATION_REQUIRED",
                "permission_to_dispatch": False}
