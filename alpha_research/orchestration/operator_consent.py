"""P4.7.2 externally signed consent *audit* and one-shot reservation.

This module deliberately contains NO signing/private-key operation and NO
production dispatch capability. An external Ed25519 signature is necessary,
but not sufficient, to prove that the operator really authorized a charge.
Trust roots, consent evidence and key revocation must be provisioned and
audited outside this repository before any production integration.
"""
import base64
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.execution_identity import inspect_execution_identity
from alpha_research.orchestration.execution_proposal import inspect_execution_proposal
from alpha_research.orchestration.mock_workspace import marker_path
from alpha_research.orchestration.simulation_attempt_admission import (
    _paths as _staging_paths, inspect_staged_simulation_attempt,
)
from alpha_research.state.fingerprint import canonical_json_bytes, fingerprint_json
from alpha_research.state.store import atomic_write_json
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError

_VERSION = 1
_CONTEXT = b"alpha_research/external-operator-consent/v1:"
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-zA-Z0-9_-]{1,80}\Z")
_OPERATION = "brain_simulation"
_RESERVED = "RESERVED_CONSENT_AUDIT_ONLY"
_RECONCILE = "RECONCILIATION_REQUIRED"
_CLAIMS = frozenset((
    "schema_version", "grant_id", "issuer_id", "key_id", "operator_approval_id",
    "operation", "task_id", "workflow", "request_sha256", "proposal_sha256",
    "model_profile_sha256", "model_governance_policy_sha256", "research_id",
    "run_id", "batch_id", "scope_authorization_id", "simulation_sha256",
    "intent_sha256", "scope", "max_attempts", "max_model_calls",
    "allow_alpha_submission", "issued_at", "expires_at",
))


def _root(orchestrator):
    return (Path(orchestrator.state_root) / "state" / "research_v1_1"
            / "operator_consent_audit")


def _paths(orchestrator, task_id):
    orchestrator._path(task_id)
    root = _root(orchestrator)
    return root, root / "consumed.json", root / (task_id + ".json")


def _utc(value):
    if not isinstance(value, str):
        raise OrchestrationError("operator consent time must be UTC ISO text")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OrchestrationError("operator consent time invalid") from exc
    if dt.tzinfo is None or dt.utcoffset() != timedelta(0):
        raise OrchestrationError("operator consent time must be UTC")
    return dt.astimezone(timezone.utc)


def _require_safe_root(orchestrator):
    root = Path(orchestrator.state_root)
    marker = marker_path(root)
    stop = root / "state" / "stop.flag"
    if marker.exists() or marker.is_symlink():
        raise OrchestrationError("consent audit prohibited in mock workspace")
    if stop.exists() or stop.is_symlink():
        raise OrchestrationError("global stop signal blocks consent reservation")


def _trust_key(trust, claims):
    """Trust policy must be supplied by a separately controlled operator."""
    if (not isinstance(trust, dict)
            or set(trust) != {"schema_version", "issuers", "revoked_grants"}
            or type(trust["schema_version"]) is not int
            or trust["schema_version"] != 1
            or not isinstance(trust["issuers"], dict)
            or not isinstance(trust["revoked_grants"], list)
            or any(not isinstance(item, str) or _HEX32.fullmatch(item) is None
                   for item in trust["revoked_grants"])
            or len(set(trust["revoked_grants"])) != len(trust["revoked_grants"])):
        raise OrchestrationError("external issuer trust policy invalid")
    if claims["grant_id"] in trust["revoked_grants"]:
        raise OrchestrationError("operator consent grant revoked")
    issuer = trust["issuers"].get(claims["issuer_id"])
    if (not isinstance(issuer, dict)
            or set(issuer) != {"key_id", "public_key_b64", "enabled", "operations"}
            or issuer["enabled"] is not True
            or issuer["key_id"] != claims["key_id"]
            or issuer["operations"] != [_OPERATION]
            or not isinstance(issuer["public_key_b64"], str)):
        raise OrchestrationError("external issuer not trusted for this operation")
    try:
        key_data = base64.b64decode(issuer["public_key_b64"], validate=True)
        if len(key_data) != 32:
            raise ValueError("wrong key length")
        return Ed25519PublicKey.from_public_bytes(key_data)
    except (ValueError, TypeError) as exc:
        raise OrchestrationError("external issuer public key invalid") from exc


def _load_index(root):
    """Validate all reciprocal entries and projections, including other tasks."""
    index_path = root / "consumed.json"
    if index_path.is_symlink():
        raise OrchestrationError("operator consent index symlink forbidden")
    projections = list(root.glob("[0-9a-f]" * 32 + ".json"))
    if not index_path.exists():
        if projections:
            raise OrchestrationError("operator consent index missing with projections")
        return {"schema_version": 1, "by_grant": {}, "by_task": {}}
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OrchestrationError("operator consent index unreadable") from exc
    if (not isinstance(index, dict)
            or set(index) != {"schema_version", "by_grant", "by_task"}
            or type(index["schema_version"]) is not int
            or index["schema_version"] != 1
            or not isinstance(index["by_grant"], dict)
            or not isinstance(index["by_task"], dict)
            or len(index["by_grant"]) != len(index["by_task"])):
        raise OrchestrationError("operator consent index schema invalid")
    for gid, entry in index["by_grant"].items():
        if (not isinstance(gid, str) or _HEX32.fullmatch(gid) is None
                or not isinstance(entry, dict)
                or set(entry) != {"task_id", "record_sha256"}
                or not isinstance(entry["task_id"], str)
                or _HEX32.fullmatch(entry["task_id"]) is None
                or not isinstance(entry["record_sha256"], str)
                or _HEX64.fullmatch(entry["record_sha256"]) is None
                or index["by_task"].get(entry["task_id"]) != gid):
            raise OrchestrationError("operator consent reciprocal index invalid")
        projection = root / (entry["task_id"] + ".json")
        if projection.is_symlink() or not projection.is_file():
            raise OrchestrationError("operator consent projection missing")
        try:
            record = json.loads(projection.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OrchestrationError("operator consent projection unreadable") from exc
        if (not isinstance(record, dict)
                or record.get("grant_id") != gid
                or record.get("task_id") != entry["task_id"]
                or record.get("status") != _RESERVED
                or record.get("permission_to_dispatch") is not False
                or fingerprint_json(record) != entry["record_sha256"]):
            raise OrchestrationError("operator consent projection drifted")
    for tid, gid in index["by_task"].items():
        if (not isinstance(tid, str) or _HEX32.fullmatch(tid) is None
                or not isinstance(gid, str)
                or not isinstance(index["by_grant"].get(gid), dict)
                or index["by_grant"][gid]["task_id"] != tid):
            raise OrchestrationError("operator consent reverse index invalid")
    if any(p.stem not in index["by_task"] or p.is_symlink()
           for p in projections):
        raise OrchestrationError("operator consent orphan projection")
    return index


def verify_external_consent(orchestrator, task_id, model_config, governance_policy,
                            receipt, trust_policy, *, now=None):
    """Verify a pinned external signature and scope. NEVER authorize dispatch."""
    _require_safe_root(orchestrator)
    task = orchestrator.load(task_id)
    if (task["status"] != "CREATED"
            or task["workflow"] not in {"super", "optimization"}
            or task["budget"] != {"limit": 1, "used": 0, "reserved": 0}):
        raise OrchestrationError("operator consent requires fresh one-attempt task")
    staged = inspect_staged_simulation_attempt(orchestrator, task_id)
    if staged["status"] != "STAGED_NO_DISPATCH":
        raise OrchestrationError("operator consent requires complete staged evidence")
    if not isinstance(receipt, dict) or set(receipt) != {"claims", "signature"}:
        raise OrchestrationError("operator consent envelope invalid")
    claims, signature = receipt["claims"], receipt["signature"]
    if not isinstance(claims, dict) or set(claims) != _CLAIMS:
        raise OrchestrationError("operator consent claims invalid")
    for name in ("grant_id", "operator_approval_id", "scope_authorization_id"):
        if not isinstance(claims[name], str) or _HEX32.fullmatch(claims[name]) is None:
            raise OrchestrationError("operator consent identifier invalid")
    for name in ("issuer_id", "key_id"):
        if not isinstance(claims[name], str) or _ID.fullmatch(claims[name]) is None:
            raise OrchestrationError("operator consent issuer identity invalid")
    for name in ("request_sha256", "proposal_sha256", "model_profile_sha256",
                 "model_governance_policy_sha256", "simulation_sha256",
                 "intent_sha256"):
        if not isinstance(claims[name], str) or _HEX64.fullmatch(claims[name]) is None:
            raise OrchestrationError("operator consent digest invalid")
    if (type(claims["schema_version"]) is not int or claims["schema_version"] != _VERSION
            or claims["operation"] != _OPERATION
            or type(claims["max_attempts"]) is not int or claims["max_attempts"] != 1
            or type(claims["max_model_calls"]) is not int
            or claims["max_model_calls"] != 0
            or claims["allow_alpha_submission"] is not False):
        raise OrchestrationError("operator consent operation/budget invalid")
    issued, expires = _utc(claims["issued_at"]), _utc(claims["expires_at"])
    clock = datetime.now(timezone.utc) if now is None else now
    if not isinstance(clock, datetime) or clock.tzinfo is None:
        raise OrchestrationError("operator consent verification clock invalid")
    clock = clock.astimezone(timezone.utc)
    if (issued > clock or expires <= clock or expires <= issued
            or expires - issued > timedelta(minutes=15)):
        raise OrchestrationError("operator consent outside approved time window")
    proposal = inspect_execution_proposal(orchestrator, task_id)
    identity = inspect_execution_identity(
        orchestrator, task_id, model_config, governance_policy)["identity"]
    journal_path, intent_path, _ = _staging_paths(orchestrator, task_id)
    if intent_path.is_symlink() or not intent_path.is_file():
        raise OrchestrationError("operator consent Intent evidence missing")
    try:
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OrchestrationError("operator consent staged evidence unreadable") from exc
    scope = {key: intent["settings"].get(key) for key in (
        "region", "delay", "universe", "instrument_type")}
    expected = {
        "task_id": task_id, "workflow": task["workflow"],
        "request_sha256": task["request_sha256"],
        "proposal_sha256": proposal["proposal_sha256"],
        "model_profile_sha256": identity["model_profile_sha256"],
        "model_governance_policy_sha256": identity["model_governance_policy_sha256"],
        "research_id": staged["research_id"],
        "run_id": intent["run_id"], "batch_id": intent["batch_id"],
        "scope_authorization_id": staged["authorization_id"],
        "simulation_sha256": staged["simulation_sha256"],
        "intent_sha256": fingerprint_json(intent), "scope": scope,
    }
    if any(claims.get(k) != value for k, value in expected.items()):
        raise OrchestrationError("operator consent scope or immutable identity drift")
    if not isinstance(signature, str):
        raise OrchestrationError("operator consent signature invalid")
    try:
        signature_bytes = base64.b64decode(signature, validate=True)
    except (ValueError, TypeError) as exc:
        raise OrchestrationError("operator consent signature malformed") from exc
    if len(signature_bytes) != 64:
        raise OrchestrationError("operator consent signature length invalid")
    key = _trust_key(trust_policy, claims)
    try:
        key.verify(signature_bytes, _CONTEXT + canonical_json_bytes(claims))
    except InvalidSignature as exc:
        raise OrchestrationError("operator consent signature mismatch") from exc
    return {
        "schema_version": _VERSION, "task_id": task_id,
        "grant_id": claims["grant_id"],
        "operator_approval_id": claims["operator_approval_id"],
        "issuer_id": claims["issuer_id"], "intent_sha256": claims["intent_sha256"],
        "receipt_sha256": fingerprint_json(receipt),
        "status": "EXTERNAL_SIGNATURE_VERIFIED_NOT_AUTHORIZED",
        "signature_verified": True,
        "permission_to_dispatch": False, "model_call_authorized": False,
        "brain_simulation_authorized": False, "alpha_submission_authorized": False,
    }


def inspect_consent_reservation(orchestrator, task_id):
    """Read-only, no grant consumption, filesystem writes or model/BRAIN I/O."""
    root, _, _ = _paths(orchestrator, task_id)
    try:
        index = _load_index(root)
    except (OrchestrationError, OSError, ValueError, KeyError, TypeError):
        return {"task_id": task_id, "status": _RECONCILE,
                "permission_to_dispatch": False}
    gid = index["by_task"].get(task_id)
    if gid is None:
        return {"task_id": task_id, "status": "NOT_RESERVED",
                "permission_to_dispatch": False}
    return {"task_id": task_id, "grant_id": gid, "status": _RESERVED,
            "permission_to_dispatch": False}


def reserve_consent_for_audit(orchestrator, task_id, model_config,
                              governance_policy, receipt, trust_policy, *,
                              now=None):
    """Durable one-shot audit reservation. NOT production grant consumption."""
    checked = verify_external_consent(
        orchestrator, task_id, model_config, governance_policy,
        receipt, trust_policy, now=now)
    root, index_path, record_path = _paths(orchestrator, task_id)
    try:
        with SingleWriterLock(root / "reservation.lock"):
            checked = verify_external_consent(
                orchestrator, task_id, model_config, governance_policy,
                receipt, trust_policy, now=now)
            _require_safe_root(orchestrator)
            index = _load_index(root)
            gid = checked["grant_id"]
            if (gid in index["by_grant"] or task_id in index["by_task"]
                    or record_path.exists() or record_path.is_symlink()):
                raise OrchestrationError("operator consent grant/task already reserved")
            record = {
                "schema_version": 1, "task_id": task_id, "grant_id": gid,
                "issuer_id": checked["issuer_id"],
                "operator_approval_id": checked["operator_approval_id"],
                "intent_sha256": checked["intent_sha256"],
                "receipt_sha256": checked["receipt_sha256"],
                "status": _RESERVED, "permission_to_dispatch": False,
                "reserved_at": datetime.now(timezone.utc).isoformat(),
            }
            index["by_grant"][gid] = {
                "task_id": task_id, "record_sha256": fingerprint_json(record),
            }
            index["by_task"][task_id] = gid
            atomic_write_json(index_path, index)  # canonical-first
            atomic_write_json(record_path, record)
            return inspect_consent_reservation(orchestrator, task_id)
    except WriterLockError as exc:
        raise OrchestrationError("operator consent reservation writer busy") from exc
