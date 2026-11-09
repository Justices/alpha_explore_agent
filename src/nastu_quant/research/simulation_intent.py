"""Durable simulation intent state machine for non-idempotent BRAIN calls.

A POST is never retried from an uncertain state. Recovery must reconcile the
platform-side operation before a caller may proceed.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from ..state_store import atomic_write_json


_TRANSITIONS = {
    "PREPARED": {"POSTING"},
    "POSTING": {"RESPONSE_RECEIVED", "OUTCOME_UNKNOWN"},
    "RESPONSE_RECEIVED": {"READ_BACK", "OUTCOME_UNKNOWN"},
    "READ_BACK": set(),
    "OUTCOME_UNKNOWN": set(),
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def create_simulation_intent(path, *, workflow, run_id, batch_id,
                             request, settings):
    """Persist the immutable request fingerprint before any platform POST."""
    if workflow not in {"super", "optimization"}:
        raise ValueError("unsupported simulation workflow")
    for name, value in (("run_id", run_id), ("batch_id", batch_id)):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} is required")
    if not isinstance(request, dict) or not isinstance(settings, dict):
        raise ValueError("request and settings must be objects")
    path = Path(path)
    if path.exists():
        raise ValueError("simulation intent already exists; refusing duplicate POST")
    payload = {"request": request, "settings": settings}
    now = _now()
    record = {
        "schema_version": 1, "workflow": workflow, "run_id": run_id,
        "batch_id": batch_id, "status": "PREPARED",
        "request_sha256": _digest(payload), "request": request,
        "settings": settings, "receipt_ref": None,
        "created_at": now, "updated_at": now,
    }
    atomic_write_json(path, record)
    return record


def transition_simulation_intent(path, *, expected_status, status,
                                 receipt_ref=None, response_sha256=None):
    """Compare-and-transition an intent; terminal/uncertain states never retry."""
    path = Path(path)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("simulation intent is missing or unreadable") from exc
    if record.get("status") != expected_status:
        raise ValueError(
            f"simulation intent state mismatch: expected {expected_status}, "
            f"found {record.get('status')}")
    if status not in _TRANSITIONS.get(expected_status, set()):
        raise ValueError(f"invalid simulation intent transition: "
                         f"{expected_status} -> {status}")
    if status == "READ_BACK":
        if not isinstance(receipt_ref, str) or not receipt_ref.strip():
            raise ValueError("READ_BACK requires a receipt reference")
        record["receipt_ref"] = receipt_ref
    if response_sha256 is not None:
        if not isinstance(response_sha256, str) or len(response_sha256) != 64:
            raise ValueError("response_sha256 must be a SHA-256 hex digest")
        record["response_sha256"] = response_sha256
    record["status"] = status
    record["updated_at"] = _now()
    atomic_write_json(path, record)
    return record
