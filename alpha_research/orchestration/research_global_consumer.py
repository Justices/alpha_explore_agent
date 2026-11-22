"""P4.19 externally signed one-shot research grant consumer ACK contract.

A signed acknowledgement is a *read-only* artifact and cannot prove that
this local trust policy was provisioned independently, that the remote
service globally serialized the claim, or that the service was contacted.
No network request, execution token, BRAIN command or paid-model call here.
"""
import base64
import re
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_only_admission import (
    _utc, verify_research_grant,
)
from alpha_research.state.fingerprint import canonical_json_bytes, fingerprint_json

_CONTEXT = b"alpha_research/research-grant-global-consumption/v1:"
_OPERATION = "consume_research_grant_once"
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_NAME = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_FIXED = frozenset({
    "schema_version", "operation", "task_id", "workflow",
    "grant_id", "operator_approval_id", "research_receipt_sha256",
    "research_scope_sha256", "model_profile_sha256",
    "price_evidence_sha256", "max_model_calls",
    "max_total_cost_usd_micros",
})
_EXTERNAL = frozenset({
    "consumer_id", "key_id", "consumption_id", "ledger_id",
    "ledger_sequence", "consumed_at", "expires_at", "outcome",
})


def build_global_consumption_request(orchestrator, task_id, model_config,
                                     governance_policy, research_receipt,
                                     research_trust, *, now=None):
    verified = verify_research_grant(
        orchestrator, task_id, model_config, governance_policy,
        research_receipt, research_trust, now=now)
    claims = research_receipt["claims"]
    fixed = {
        "schema_version": 1,
        "operation": _OPERATION,
        "task_id": task_id,
        "workflow": claims["workflow"],
        "grant_id": claims["grant_id"],
        "operator_approval_id": claims["operator_approval_id"],
        "research_receipt_sha256": verified["receipt_sha256"],
        "research_scope_sha256": fingerprint_json(claims["scope"]),
        "model_profile_sha256": claims["model_profile_sha256"],
        "price_evidence_sha256": claims["price_evidence_sha256"],
        "max_model_calls": claims["max_model_calls"],
        "max_total_cost_usd_micros": claims["max_total_cost_usd_micros"],
    }
    if set(fixed) != _FIXED:
        raise OrchestrationError("global research consumer schema drift")
    return {
        "schema_version": 1,
        "task_id": task_id,
        "status": "AWAITING_INDEPENDENT_GLOBAL_CONSUMER",
        "fixed_claims": fixed,
        "fixed_claims_sha256": fingerprint_json(fixed),
        "external_fields": sorted(_EXTERNAL),
        "external_service_invoked": False,
        "independent_key_custody_attested": False,
        "global_single_use_attested": False,
        "model_call_authorized": False,
        "brain_read_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "permission_to_dispatch": False,
    }


def verify_global_consumption_ack(
        orchestrator, task_id, model_config, governance_policy,
        research_receipt, research_trust, ack, consumer_trust, *, now=None):
    """Verify acknowledgement cryptographically, NOT an execution permission."""
    request = build_global_consumption_request(
        orchestrator, task_id, model_config, governance_policy,
        research_receipt, research_trust, now=now)
    fixed = request["fixed_claims"]
    if (not isinstance(ack, dict) or set(ack) != {"claims", "signature"}
            or not isinstance(ack["claims"], dict)
            or set(ack["claims"]) != _FIXED | _EXTERNAL):
        raise OrchestrationError("global consumption receipt schema invalid")
    claims = ack["claims"]
    if (any(type(claims[k]) is not type(v) or claims[k] != v
            for k, v in fixed.items())
            or claims["outcome"] != "CONSUMED_ONCE"
            or type(claims["ledger_sequence"]) is not int
            or claims["ledger_sequence"] < 1):
        raise OrchestrationError("global consumer acknowledgement binding invalid")
    for key in ("consumer_id", "key_id", "ledger_id"):
        if (not isinstance(claims[key], str)
                or _NAME.fullmatch(claims[key]) is None):
            raise OrchestrationError("global consumer identity malformed")
    if (not isinstance(claims["consumption_id"], str)
            or _HEX32.fullmatch(claims["consumption_id"]) is None):
        raise OrchestrationError("global consumption ID invalid")
    clock = datetime.now(timezone.utc) if now is None else now
    if not isinstance(clock, datetime) or clock.tzinfo is None:
        raise OrchestrationError("global consumption clock invalid")
    clock = clock.astimezone(timezone.utc)
    start, end = _utc(claims["consumed_at"]), _utc(claims["expires_at"])
    if (start > clock or end <= clock or end <= start
            or end - start > timedelta(minutes=2)):
        raise OrchestrationError("global consumer acknowledgement expired or future")
    if (not isinstance(consumer_trust, dict)
            or set(consumer_trust) != {
                "schema_version", "consumers", "revoked_consumptions"}
            or type(consumer_trust["schema_version"]) is not int
            or consumer_trust["schema_version"] != 1
            or not isinstance(consumer_trust["consumers"], dict)
            or not isinstance(consumer_trust["revoked_consumptions"], list)
            or any(not isinstance(s, str) or _HEX32.fullmatch(s) is None
                   for s in consumer_trust["revoked_consumptions"])):
        raise OrchestrationError("global consumer trust policy invalid")
    if claims["consumption_id"] in consumer_trust["revoked_consumptions"]:
        raise OrchestrationError("global consumer acknowledgement revoked")
    entry = consumer_trust["consumers"].get(claims["consumer_id"])
    if (not isinstance(entry, dict)
            or set(entry) != {"key_id", "public_key_b64", "enabled", "operation"}
            or entry["key_id"] != claims["key_id"]
            or entry["operation"] != _OPERATION
            or entry["enabled"] is not True
            or not isinstance(entry["public_key_b64"], str)):
        raise OrchestrationError("global consumer trust root or operation invalid")
    try:
        pub = base64.b64decode(entry["public_key_b64"], validate=True)
        sig = base64.b64decode(ack["signature"], validate=True)
        if len(pub) != 32 or len(sig) != 64:
            raise ValueError("invalid Ed25519 length")
        Ed25519PublicKey.from_public_bytes(pub).verify(
            sig, _CONTEXT + canonical_json_bytes(claims))
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise OrchestrationError("global consumption acknowledgement signature invalid") from exc
    return {
        "schema_version": 1, "task_id": task_id,
        "status": "GLOBAL_ACK_SIGNATURE_VERIFIED_AUDIT_ONLY",
        "grant_id": claims["grant_id"],
        "consumption_id": claims["consumption_id"],
        "ack_sha256": fingerprint_json(ack),
        "global_single_use_attested": False,
        "independent_key_custody_attested": False,
        "remote_service_called": False,
        "model_call_authorized": False,
        "brain_read_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "permission_to_dispatch": False,
        "automatic_retry_allowed": False,
    }


def consume_global_research_grant(*_args, **_kwargs):
    """No production consumer provisioned. Never fall back to local trust."""
    raise OrchestrationError(
        "independently trusted global research grant consumer is not configured")
