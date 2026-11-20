"""Offline-only signed approval receipt verification and single-use reservation.

An externally issued HMAC-SHA256 record proves possession of a separate
approval-verifier secret; it does NOT establish user consent or grant BRAIN
permissions. No signing function, provider, CLI, or gateway call lives here.
A future trusted approval issuer and a separately reviewed execution gate
are required before any real operation can be authorized.
"""
import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.execution_proposal import inspect_execution_proposal
from alpha_research.state.fingerprint import canonical_json_bytes, fingerprint_json
from alpha_research.state.store import atomic_write_json
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError

SCHEMA_VERSION = 1
SIGNING_CONTEXT = b"alpha_research/approval-review/v1:"
KEY_ENV = "ALPHA_RESEARCH_APPROVAL_VERIFY_KEY"
_ALLOWED_OPERATIONS = ("review_execution_proposal",)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _parse_instant(value):
    if not isinstance(value, str):
        raise OrchestrationError("approval timestamp must be a UTC ISO datetime")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OrchestrationError("approval timestamp is malformed") from exc
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise OrchestrationError("approval timestamp must be UTC")
    return result.astimezone(timezone.utc)


def _claims(orchestrator, receipt, *, now, expected_model_profile_sha256):
    if not isinstance(receipt, dict) or set(receipt) != {"claims", "signature"}:
        raise OrchestrationError("approval receipt has invalid envelope")
    claims, signature = receipt["claims"], receipt["signature"]
    keys = {"schema_version", "approval_id", "issuer", "task_id", "workflow",
            "request_sha256", "proposal_sha256", "model_profile_sha256",
            "operation", "max_execution_attempts", "issued_at", "expires_at"}
    if not isinstance(claims, dict) or set(claims) != keys:
        raise OrchestrationError("approval claims have invalid schema")
    if claims["schema_version"] != SCHEMA_VERSION:
        raise OrchestrationError("approval schema version not supported")
    if not isinstance(claims["approval_id"], str) or not _HEX32.fullmatch(claims["approval_id"]):
        raise OrchestrationError("approval ID is invalid")
    if not isinstance(claims["issuer"], str) or not claims["issuer"].strip():
        raise OrchestrationError("approval issuer is required")
    if claims["operation"] not in _ALLOWED_OPERATIONS:
        raise OrchestrationError("approval operation is not allowed")
    if type(claims["max_execution_attempts"]) is not int or claims["max_execution_attempts"] != 1:
        raise OrchestrationError("approval budget is invalid")
    for name in ("request_sha256", "proposal_sha256", "model_profile_sha256"):
        if not isinstance(claims[name], str) or not _HEX64.fullmatch(claims[name]):
            raise OrchestrationError("approval identity hash is invalid")
    if not isinstance(signature, str) or not _HEX64.fullmatch(signature):
        raise OrchestrationError("approval signature format is invalid")
    if not isinstance(expected_model_profile_sha256, str) or not _HEX64.fullmatch(expected_model_profile_sha256):
        raise OrchestrationError("expected model profile hash is required")
    issued, expires = _parse_instant(claims["issued_at"]), _parse_instant(claims["expires_at"])
    if issued > now or expires <= now or expires <= issued or expires - issued > timedelta(minutes=30):
        raise OrchestrationError("approval is expired, premature or excessive duration")
    proposal = inspect_execution_proposal(orchestrator, claims["task_id"])
    pinned = proposal["proposal"]
    if (claims["task_id"] != pinned["task_id"] or claims["workflow"] != pinned["workflow"]
            or claims["request_sha256"] != pinned["request_sha256"]
            or claims["proposal_sha256"] != proposal["proposal_sha256"]
            or claims["model_profile_sha256"] != expected_model_profile_sha256):
        raise OrchestrationError("approval binding does not match saved task and model profile")
    return claims, signature


def verify_review_receipt(orchestrator, receipt, *, model_profile_sha256, environ=None, now=None):
    """Only attest a signed *review* claim; NEVER grant a production call."""
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None:
        raise OrchestrationError("approval verification clock must be timezone aware")
    current = current.astimezone(timezone.utc)
    claims, signature = _claims(
        orchestrator, receipt, now=current,
        expected_model_profile_sha256=model_profile_sha256)
    environment = os.environ if environ is None else environ
    key = environment.get(KEY_ENV)
    if not isinstance(key, str) or len(key) < 32:
        raise OrchestrationError("trusted approval verifier key is unavailable")
    actual = hmac.new(
        key.encode("utf-8"), SIGNING_CONTEXT + canonical_json_bytes(claims),
        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(actual, signature):
        raise OrchestrationError("approval signature does not verify")
    return {"approval_id": claims["approval_id"], "task_id": claims["task_id"],
            "operation": claims["operation"], "receipt_sha256": fingerprint_json(receipt),
            "verified": True, "simulation_authorized": False,
            "model_call_authorized": False, "alpha_submission_authorized": False}


def reserve_review_receipt(orchestrator, receipt, *, model_profile_sha256, environ=None, now=None):
    """Durably consume one approval-review ID under a global process lock.

    Reservation is an audit event, not a dispatch capability. An interrupted
    write or retry is conservatively rejected; no automatic replay occurs.
    """
    info = verify_review_receipt(
        orchestrator, receipt, model_profile_sha256=model_profile_sha256,
        environ=environ, now=now)
    root = Path(orchestrator.state_root) / "state" / "research_v1_1" / "approval_reviews"
    path = root / (info["approval_id"] + ".json")
    try:
        with SingleWriterLock(root / "reviews.lock"):
            # Re-evaluate after acquiring lock to reject task/proposal changes.
            info = verify_review_receipt(
                orchestrator, receipt, model_profile_sha256=model_profile_sha256,
                environ=environ, now=now)
            if path.exists():
                raise OrchestrationError("approval review ID already consumed")
            record = {"schema_version": SCHEMA_VERSION,
                      "status": "RESERVED_REVIEW_ONLY",
                      **info,
                      "recorded_at": datetime.now(timezone.utc).isoformat()}
            atomic_write_json(path, record)
            return record
    except WriterLockError as exc:
        raise OrchestrationError("approval review ledger is busy") from exc
