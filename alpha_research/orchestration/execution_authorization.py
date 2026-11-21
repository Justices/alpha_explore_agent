"""P3 execution authorization receipt contract (verification only).

A separate trusted issuer is NOT supplied by this application. This module
does not sign receipts, persist grants, create sessions, or authorize calls.
A successfully verified receipt remains INSPECTED_NOT_EXECUTABLE while the
production gateway/admission adapter is disabled.
"""
import hashlib
import hmac
import os
import re
from datetime import datetime, timedelta, timezone

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.execution_identity import (
    inspect_execution_identity,
)
from alpha_research.orchestration.execution_proposal import (
    inspect_execution_proposal,
)
from alpha_research.state.fingerprint import (
    canonical_json_bytes, fingerprint_json,
)

VERSION = 1
CONTEXT = b"alpha_research/execution-authorization/v1:"
VERIFY_KEY_ENV = "ALPHA_RESEARCH_EXECUTION_VERIFY_KEY"
HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
OPERATIONS = frozenset((
    "model_completion", "brain_simulation", "brain_readback",
))
REQUIRED_CLAIMS = frozenset((
    "schema_version", "authorization_id", "issuer", "task_id", "workflow",
    "request_sha256", "proposal_sha256", "model_profile_sha256",
    "model_governance_policy_sha256", "operation", "max_attempts",
    "issued_at", "expires_at",
))


def _utc_datetime(value):
    if not isinstance(value, str):
        raise OrchestrationError("execution receipt timestamp must be a UTC ISO string")
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OrchestrationError("execution receipt timestamp is malformed") from exc
    if when.tzinfo is None or when.utcoffset() != timedelta(0):
        raise OrchestrationError("execution receipt timestamps must be UTC")
    return when.astimezone(timezone.utc)


def inspect_execution_receipt(orchestrator, task_id, model_config,
                              governance_policy, receipt, *, environ=None,
                              now=None):
    """Verify an external signature AND exact pinned scope, without admission.

    No read-only inspection may return a positive execution permission.
    A verification key proves signature provenance only, never user consent.
    """
    if not isinstance(receipt, dict) or set(receipt) != {"claims", "signature"}:
        raise OrchestrationError("execution receipt envelope invalid")
    claims, signature = receipt["claims"], receipt["signature"]
    if not isinstance(claims, dict) or set(claims) != REQUIRED_CLAIMS:
        raise OrchestrationError("execution receipt claims invalid")
    if type(claims["schema_version"]) is not int or claims["schema_version"] != VERSION:
        raise OrchestrationError("execution receipt version invalid")
    if (not isinstance(claims["authorization_id"], str)
            or not HEX32.fullmatch(claims["authorization_id"])):
        raise OrchestrationError("execution authorization ID invalid")
    if not isinstance(claims["issuer"], str) or not claims["issuer"].strip():
        raise OrchestrationError("execution authorization issuer required")
    if claims["task_id"] != task_id:
        raise OrchestrationError("execution receipt task ID mismatch")
    if claims["operation"] not in OPERATIONS:
        raise OrchestrationError("execution receipt operation is not supported")
    if type(claims["max_attempts"]) is not int or claims["max_attempts"] != 1:
        raise OrchestrationError("execution receipt must allow exactly one attempt")
    for field in ("request_sha256", "proposal_sha256", "model_profile_sha256",
                  "model_governance_policy_sha256"):
        if not isinstance(claims[field], str) or not HEX64.fullmatch(claims[field]):
            raise OrchestrationError("execution receipt hash format invalid")
    if not isinstance(signature, str) or not HEX64.fullmatch(signature):
        raise OrchestrationError("execution receipt signature format invalid")

    current = datetime.now(timezone.utc) if now is None else now
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise OrchestrationError("execution verification clock must be timezone aware")
    current = current.astimezone(timezone.utc)
    issued = _utc_datetime(claims["issued_at"])
    expires = _utc_datetime(claims["expires_at"])
    if issued > current or expires <= current or expires <= issued:
        raise OrchestrationError("execution authorization outside valid time window")
    if expires - issued > timedelta(minutes=15):
        raise OrchestrationError("execution authorization TTL exceeds 15 minutes")

    task = orchestrator.load(task_id)
    if task["status"] != "CREATED" or task["budget"] != {
            "limit": 1, "used": 0, "reserved": 0}:
        raise OrchestrationError("execution receipt requires a fresh prepared task")
    proposal = inspect_execution_proposal(orchestrator, task_id)
    identity = inspect_execution_identity(
        orchestrator, task_id, model_config, governance_policy)
    pinned = identity["identity"]
    expected = {
        "workflow": task["workflow"],
        "request_sha256": task["request_sha256"],
        "proposal_sha256": proposal["proposal_sha256"],
        "model_profile_sha256": pinned["model_profile_sha256"],
        "model_governance_policy_sha256": pinned["model_governance_policy_sha256"],
    }
    if any(claims[name] != expected[name] for name in expected):
        raise OrchestrationError("execution receipt identity or governance drift")

    environment = os.environ if environ is None else environ
    key = environment.get(VERIFY_KEY_ENV)
    if not isinstance(key, str) or len(key) < 32:
        raise OrchestrationError("trusted execution receipt verifier key unavailable")
    expected_sig = hmac.new(
        key.encode("utf-8"),
        CONTEXT + canonical_json_bytes(claims), hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_sig, signature):
        raise OrchestrationError("execution receipt signature invalid")
    return {
        "task_id": task_id,
        "authorization_id": claims["authorization_id"],
        "operation": claims["operation"],
        "receipt_sha256": fingerprint_json(receipt),
        "status": "INSPECTED_NOT_EXECUTABLE",
        "signature_verified": True,
        "permission_to_dispatch": False,
        "model_call_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
    }
