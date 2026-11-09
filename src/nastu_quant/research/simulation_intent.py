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
    # Reconciliation may settle an unknown outcome; reposting is never allowed.
    "OUTCOME_UNKNOWN": {"RESPONSE_RECEIVED"},
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def create_simulation_intent(path, *, workflow, run_id, batch_id,
                             request, settings, metadata=None):
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
    if metadata is not None and not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    payload = {"request": request, "settings": settings}
    now = _now()
    record = {
        "schema_version": 1, "workflow": workflow, "run_id": run_id,
        "batch_id": batch_id, "status": "PREPARED",
        "request_sha256": _digest(payload), "request": request,
        "settings": settings, "metadata": metadata or {}, "receipt_ref": None,
        "created_at": now, "updated_at": now,
    }
    atomic_write_json(path, record)
    return record


def transition_simulation_intent(path, *, expected_status, status,
                                 receipt_ref=None, response_sha256=None,
                                 response_ref=None):
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
    if response_ref is not None:
        if not isinstance(response_ref, str) or not response_ref.strip():
            raise ValueError("response_ref must be a nonempty path")
        record["response_ref"] = response_ref
    if response_sha256 is not None:
        if not isinstance(response_sha256, str) or len(response_sha256) != 64:
            raise ValueError("response_sha256 must be a SHA-256 hex digest")
        record["response_sha256"] = response_sha256
    record["status"] = status
    record["updated_at"] = _now()
    atomic_write_json(path, record)
    return record


def recover_simulation_intent(gateway, state_root, intent_path):
    """Reconcile a possibly submitted batch using the platform's persisted runner.

    This calls only resume_simulation_batch. It never calls simulate_batch or
    simulate_super and therefore cannot create a second platform submission.
    """
    from datetime import timedelta
    from .records import ResearchRecordStore
    from .audit import record_pending_audit

    path = Path(intent_path).resolve()
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("simulation intent is unreadable") from exc
    status = record.get("status")
    if status == "READ_BACK":
        return {"status": status, "receipt_ref": record.get("receipt_ref"),
                "reconciled": True}
    if status not in {"POSTING", "OUTCOME_UNKNOWN", "RESPONSE_RECEIVED"}:
        raise ValueError(f"intent status is not recoverable: {status}")
    workflow = record.get("workflow")
    metadata = record.get("metadata") or {}
    request = record.get("request")
    run_id, batch_id = record.get("run_id"), record.get("batch_id")
    if not isinstance(request, dict) or not run_id or not batch_id:
        raise ValueError("intent lacks platform recovery identifiers")
    label = "super" if workflow == "super" or "selection" in request else "production"
    gateway.call("set_case", case_key=run_id, run_id=run_id)
    # The runtime verifies its local request/admission hashes and only resumes
    # the recorded platform location. A possibly-posted batch is never reposted.
    result = gateway.call("resume_simulation_batch", case_key=run_id,
                          run_id=run_id, batch_id=batch_id, label=label)
    rows = result.get("results") if isinstance(result, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or not rows[0].get("alpha_id"):
        raise ValueError("platform recovery did not return one complete Alpha readback")
    response_path = path.with_name(f"{run_id}_{batch_id}_recovered_response.json")
    receipt_path = path.with_name(f"{run_id}_{batch_id}_recovered_readback.json")
    atomic_write_json(response_path, result)
    atomic_write_json(receipt_path, rows[0])
    response_bytes = json.dumps(result, ensure_ascii=False, sort_keys=True,
                                default=str).encode("utf-8")
    digest = hashlib.sha256(response_bytes).hexdigest()
    if status != "RESPONSE_RECEIVED":
        transition_simulation_intent(
            path, expected_status=status, status="RESPONSE_RECEIVED",
            response_sha256=digest, response_ref=str(response_path))
    transition_simulation_intent(
        path, expected_status="RESPONSE_RECEIVED", status="READ_BACK",
        receipt_ref=str(receipt_path), response_sha256=digest,
        response_ref=str(response_path))
    store = ResearchRecordStore(state_root)
    if workflow == "super":
        shared = store.get("super_run", run_id)
        previous = shared["updated_at"]
        now = datetime.now(timezone.utc)
        last = datetime.fromisoformat(previous)
        if now <= last:
            now = last + timedelta(microseconds=1)
        shared.update(status="simulated_pending_audit",
                      candidate_alpha_id=rows[0]["alpha_id"],
                      simulation_intent_ref=str(path),
                      readback_ref=str(receipt_path),
                      budget={"limits": {"experiments": 1},
                              "reserved": {"experiments": 0},
                              "used": {"experiments": 1}},
                      updated_at=now.isoformat())
        store.put("super_run", shared, expected_updated_at=previous)
        platform_path = Path(shared["platform_record_ref"])
        platform_run = json.loads(platform_path.read_text(encoding="utf-8"))
        platform_run.update(status="simulated_pending_audit",
                            alpha_id=rows[0]["alpha_id"],
                            runner=result.get("runner"),
                            readback_ref=str(receipt_path),
                            simulation_intent_ref=str(path))
        atomic_write_json(platform_path, platform_run)
    else:
        branch_dir = store.root / "candidate_branch"
        matches = []
        if branch_dir.exists():
            for candidate_path in branch_dir.glob("*.json"):
                candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
                if candidate.get("simulation_intent_ref") == str(path):
                    matches.append(candidate)
        if len(matches) != 1:
            raise ValueError("optimization branch cannot be uniquely linked to intent")
        branch = matches[0]
        source_snapshot = json.loads(
            Path(branch["source_snapshot_ref"]).read_text(encoding="utf-8"))
        latest_source = gateway.call("get_alpha_details",
                                     alpha_id=branch["source_alpha_id"])
        def source_fingerprint(value):
            family = str(value.get("type") or "").upper()
            settings = value.get("settings") if isinstance(value.get("settings"), dict) else {}
            def code(item):
                return item.get("code") if isinstance(item, dict) else item
            if family == "REGULAR":
                definition = {"regular": code(value.get("regular"))}
            else:
                block = value.get("super") if isinstance(value.get("super"), dict) else {}
                definition = {"selection": code(value.get("selection") or block.get("selection")),
                              "combo": code(value.get("combo") or block.get("combo"))}
            return hashlib.sha256(json.dumps(
                {"id": value.get("id"), "type": family, "settings": settings,
                 **definition}, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest()
        if (not isinstance(latest_source, dict) or
                source_fingerprint(latest_source) !=
                branch.get("source_properties_sha256")):
            raise ValueError("source Alpha definition is not verifiably unchanged")
        audit_path = path.with_name(f"{run_id}_recovered_audit.json")
        audit = record_pending_audit(
            rows[0].get("details") or rows[0], workflow="optimization",
            source=source_snapshot)
        atomic_write_json(audit_path, audit)
        report_path = path.with_name(f"{run_id}_recovered_optimization_report.json")
        report = {
            "schema_version": 1, "workflow": "optimization",
            "source_alpha_id": branch["source_alpha_id"],
            "source_type": branch["source_type"],
            "source_snapshot_ref": branch["source_snapshot_ref"],
            "source_snapshot_sha256": branch["source_snapshot_sha256"],
            "source_properties_sha256": branch["source_properties_sha256"],
            "candidate_branch_id": branch["branch_id"],
            "candidate_alpha_id": rows[0]["alpha_id"],
            "objective": branch["objective"], "rationale": branch["rationale"],
            "changes": branch["changes"], "simulation_intent_ref": str(path),
            "readback_ref": str(receipt_path), "audit_ref": str(audit_path),
            "source_unchanged_verified": True,
            "candidate_status": "simulated_pending_audit",
            "pending": audit["pending"], "created_at": _now()}
        atomic_write_json(report_path, report)
        previous = branch["updated_at"]
        now = datetime.now(timezone.utc)
        last = datetime.fromisoformat(previous)
        if now <= last:
            now = last + timedelta(microseconds=1)
        branch.update(status="simulated_pending_audit",
                      receipt_ref=str(receipt_path), audit_ref=str(audit_path),
                      report_ref=str(report_path),
                      candidate_alpha_id=rows[0]["alpha_id"],
                      source_unchanged_verified=True,
                      budget={"limits": {"experiments": 1},
                              "reserved": {"experiments": 0},
                              "used": {"experiments": 1}},
                      updated_at=now.isoformat())
        store.put("candidate_branch", branch,
                  expected_updated_at=previous)
    return {"status": "simulated_pending_audit",
            "candidate_alpha_id": rows[0]["alpha_id"],
            "receipt_ref": str(receipt_path), "reconciled": True}
