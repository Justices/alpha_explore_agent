"""P4.10 bind a local BRAIN read-only observation to an exact Task scope.

This is a negative-only proof-of-observation report. No self-provided local
checksum can attest the BRAIN account, issuer, remote operation or authority.
Never return an executable token, a live grant or a positive dispatch signal.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alpha_research.orchestration.platform_read_evidence import (
    inspect_platform_read_evidence,
)
from alpha_research.orchestration.simulation_attempt_admission import (
    inspect_staged_simulation_attempt, _paths as _staging_paths,
)

_MAX_AGE = timedelta(hours=24)
_SCOPE = ("region", "delay", "universe")


def _required_scope(orchestrator, task):
    request = task["request"]
    # Task scope is always authoritative for explicitly supplied constraints.
    required = {key: request.get(key) for key in _SCOPE}
    if task["workflow"] in {"super", "optimization"}:
        staged = inspect_staged_simulation_attempt(
            orchestrator, task["task_id"])
        if staged["status"] == "RECONCILIATION_REQUIRED":
            return None, "SIMULATION_STAGING_RECONCILIATION_REQUIRED"
        if staged["status"] == "STAGED_NO_DISPATCH":
            _, path, _ = _staging_paths(orchestrator, task["task_id"])
            if path.is_symlink() or not path.is_file():
                return None, "SIMULATION_INTENT_NOT_READABLE"
            try:
                intent = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None, "SIMULATION_INTENT_NOT_READABLE"
            if (not isinstance(intent, dict)
                    or intent.get("status") != "PREPARED"
                    or not isinstance(intent.get("settings"), dict)
                    or intent.get("workflow") != task["workflow"]):
                return None, "SIMULATION_INTENT_NOT_PREPARED"
            settings = intent["settings"]
            if settings.get("instrument_type") != "EQUITY":
                return None, "SIMULATION_INSTRUMENT_SCOPE_MISMATCH"
            for key in _SCOPE:
                value = settings.get(key)
                if required[key] is not None and required[key] != value:
                    return None, "TASK_AND_SIMULATION_SCOPE_MISMATCH"
                required[key] = value
    if (not isinstance(required["region"], str)
            or type(required["delay"]) is not int
            or not isinstance(required["universe"], str)
            or not required["region"] or not required["universe"]):
        return None, "TASK_SCOPE_NOT_EXACT"
    return required, None


def inspect_platform_read_scope_binding(orchestrator, task_id, evidence_id,
                                        *, now=None):
    """Never creates files or talks to models/BRAIN. No implied authorization."""
    task = orchestrator.load(task_id)
    base = {
        "schema_version": 1, "task_id": task_id,
        "evidence_record_id": evidence_id,
        "audit_mode": "READ_ONLY",
        "local_observation_only": True,
        "platform_account_verified": False,
        "production_authorized": False,
        "brain_simulation_authorized": False,
        "permission_to_dispatch": False,
        "automatic_retry_allowed": False,
        "brain_invoked": False, "provider_invoked": False,
    }
    if evidence_id is None:
        return {**base, "status": "NOT_SUPPLIED"}
    try:
        evidence = inspect_platform_read_evidence(
            orchestrator.state_root, evidence_id)
    except (TypeError, ValueError):
        return {**base, "status": "EVIDENCE_MISSING_OR_CHANGED"}
    if evidence["status"] != "LOCAL_READ_OBSERVED_NOT_ATTESTED":
        return {**base, "status": "EVIDENCE_MISSING_OR_CHANGED"}
    clock = datetime.now(timezone.utc) if now is None else now
    try:
        stamped = datetime.fromisoformat(evidence["recorded_at"])
        if (not isinstance(clock, datetime) or clock.tzinfo is None
                or stamped.tzinfo is None
                or stamped.utcoffset() != timedelta(0)
                or clock < stamped
                or clock - stamped > _MAX_AGE):
            return {**base, "status": "LOCAL_EVIDENCE_STALE_OR_INVALID_TIME"}
    except (TypeError, ValueError, OverflowError):
        return {**base, "status": "LOCAL_EVIDENCE_STALE_OR_INVALID_TIME"}
    required, reason = _required_scope(orchestrator, task)
    if reason:
        return {**base, "status": reason}
    if required != evidence["scope"]:
        return {**base, "status": "LOCAL_SCOPE_MISMATCH",
                "expected_scope": required, "observed_scope": evidence["scope"]}
    return {
        **base, "status": "LOCAL_SCOPE_MATCH_UNATTESTED",
        "scope": dict(required),
        "dataset_count": evidence["dataset_count"],
        "local_integrity_verified": True,
    }
