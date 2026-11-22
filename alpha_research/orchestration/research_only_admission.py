"""P4.16 research-only operator grant audit and deterministic cost envelope.

This is a NON-EXECUTABLE verifier for future read-only BRAIN + paid-model
candidate preparation. It cannot connect to a provider or Gateway, reserve a
call, sign consent, or authorize a real dispatch. An externally supplied trust
policy is not proof of independent key custody; pricing assertions likewise
require external attestation before they may become executable authority.
Simulation and Alpha submission are independently forbidden.
"""
import base64
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.execution_identity import inspect_execution_identity
from alpha_research.orchestration.execution_proposal import inspect_execution_proposal
from alpha_research.orchestration.mock_workspace import marker_path
from alpha_research.state.fingerprint import canonical_json_bytes, fingerprint_json
from alpha_research.agents.governance import validate_policy

_VERSION = 1
_CONTEXT = b"alpha_research/research-preparation-only-consent/v1:"
_OPERATION = "research_prepare_model_readonly"
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_ISSUER_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_MAX_VALIDITY = timedelta(minutes=15)
_MAX_APPROVED_USD_MICROS = 100_000_000  # $100 ceiling on grants, not a price claim
_SCOPE = ("region", "delay", "universe", "instrument_type")
_FIXED = frozenset({
    "schema_version", "operation", "task_id", "workflow",
    "request_sha256", "proposal_sha256",
    "model_profile_sha256", "model_governance_policy_sha256",
    "scope", "max_model_calls", "max_reported_tokens",
    "allow_brain_simulation", "allow_alpha_submission",
})
_ISSUER = frozenset({
    "grant_id", "issuer_id", "key_id", "operator_approval_id",
    "issued_at", "expires_at", "price_evidence_sha256",
    "currency", "input_rate_micros_per_million",
    "output_rate_micros_per_million",
    "max_input_tokens_per_call", "max_output_tokens_per_call",
    "max_total_cost_usd_micros",
})
_CLAIMS = _FIXED | _ISSUER


def _safe_task(orchestrator, task_id):
    task = orchestrator.load(task_id)
    root = Path(orchestrator.state_root)
    stop = root / "state" / "stop.flag"
    marker = marker_path(root)
    if marker.exists() or marker.is_symlink():
        raise OrchestrationError("real research approval prohibited in mock workspace")
    if stop.exists() or stop.is_symlink():
        raise OrchestrationError("research approval blocked by global stop")
    if (task["status"] != "CREATED"
            or task["workflow"] not in {"super", "optimization"}
            or task["budget"] != {"limit": 1, "used": 0, "reserved": 0}
            or task.get("recovery_target") is not None
            or task.get("research_identity") is not None
            or task.get("checkpoints") != {}):
        raise OrchestrationError("research approval requires fresh unexecuted Task")
    req = task["request"]
    scope = {key: (req.get(key) if key != "instrument_type" else "EQUITY")
             for key in _SCOPE}
    if (not isinstance(scope["region"], str)
            or re.fullmatch(r"[A-Z]{3,8}", scope["region"]) is None
            or type(scope["delay"]) is not int
            or scope["delay"] not in (0, 1)
            or not isinstance(scope["universe"], str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,40}", scope["universe"]) is None):
        raise OrchestrationError("research approval requires exact pinned scope")
    if (task["workflow"] == "optimization"
            and not isinstance(req.get("alpha_id"), str)):
        raise OrchestrationError("optimization requires pinned source Alpha")
    return task, scope


def expected_research_consent_fields(orchestrator, task_id, model_config,
                                    governance_policy):
    """Only immutable, externally reviewable non-secret task/role fields."""
    task, scope = _safe_task(orchestrator, task_id)
    proposal = inspect_execution_proposal(orchestrator, task_id)
    identity = inspect_execution_identity(
        orchestrator, task_id, model_config, governance_policy)["identity"]
    policy = validate_policy(governance_policy)
    return {
        "schema_version": _VERSION, "operation": _OPERATION,
        "task_id": task_id, "workflow": task["workflow"],
        "request_sha256": task["request_sha256"],
        "proposal_sha256": proposal["proposal_sha256"],
        "model_profile_sha256": identity["model_profile_sha256"],
        "model_governance_policy_sha256":
            identity["model_governance_policy_sha256"],
        "scope": scope,
        "max_model_calls": policy["max_calls"],
        "max_reported_tokens": policy["max_reported_tokens"],
        "allow_brain_simulation": False,
        "allow_alpha_submission": False,
    }


def build_research_issuer_request(orchestrator, task_id, model_config,
                                  governance_policy):
    """Read-only claim schema; never supplies/creates keys, grants or a rate."""
    fixed = expected_research_consent_fields(
        orchestrator, task_id, model_config, governance_policy)
    if set(fixed) != _FIXED:
        raise OrchestrationError("research grant fixed-claim schema drift")
    return {
        "schema_version": 1, "task_id": task_id,
        "status": "AWAITING_INDEPENDENT_RESEARCH_APPROVAL",
        "operation": _OPERATION,
        "fixed_claims": fixed,
        "fixed_claims_sha256": fingerprint_json(fixed),
        "issuer_supplied_fields": sorted(_ISSUER),
        "pricing_evidence_required": True,
        "provider_price_independently_attested": False,
        "independent_key_custody_attested": False,
        "permission_to_dispatch": False,
        "model_call_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "provider_invoked": False,
        "brain_invoked": False,
    }


def _utc(value):
    if not isinstance(value, str):
        raise OrchestrationError("research consent UTC time invalid")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OrchestrationError("research consent UTC time invalid") from exc
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        raise OrchestrationError("research consent must use UTC")
    return stamp.astimezone(timezone.utc)


def _trusted_public_key(trust, claims):
    if (not isinstance(trust, dict)
            or set(trust) != {"schema_version", "issuers", "revoked_grants"}
            or type(trust["schema_version"]) is not int
            or trust["schema_version"] != 1
            or not isinstance(trust["issuers"], dict)
            or not isinstance(trust["revoked_grants"], list)
            or any(not isinstance(g, str) or not _HEX32.fullmatch(g)
                   for g in trust["revoked_grants"])
            or len(set(trust["revoked_grants"])) != len(trust["revoked_grants"])):
        raise OrchestrationError("research operator trust policy invalid")
    if claims["grant_id"] in trust["revoked_grants"]:
        raise OrchestrationError("research grant revoked")
    issuer = trust["issuers"].get(claims["issuer_id"])
    if (not isinstance(issuer, dict)
            or set(issuer) != {"key_id", "public_key_b64", "enabled", "operations"}
            or issuer["key_id"] != claims["key_id"]
            or issuer["enabled"] is not True
            or issuer["operations"] != [_OPERATION]
            or not isinstance(issuer["public_key_b64"], str)):
        raise OrchestrationError("research grant issuer not trusted for scope")
    try:
        raw = base64.b64decode(issuer["public_key_b64"], validate=True)
        if len(raw) != 32:
            raise ValueError("unexpected key length")
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as exc:
        raise OrchestrationError("research issuer public key invalid") from exc


def _cost_envelope(claims):
    """Strict integer arithmetic: cents/FX are not inferred from usage caps."""
    for key in ("max_input_tokens_per_call", "max_output_tokens_per_call",
                "input_rate_micros_per_million",
                "output_rate_micros_per_million",
                "max_total_cost_usd_micros"):
        amount = claims[key]
        if type(amount) is not int or amount <= 0 or amount > 10_000_000_000:
            raise OrchestrationError("research pricing envelope contains invalid limit")
    if (claims["currency"] != "USD"
            or not isinstance(claims["price_evidence_sha256"], str)
            or not _HEX64.fullmatch(claims["price_evidence_sha256"])
            or claims["max_total_cost_usd_micros"] > _MAX_APPROVED_USD_MICROS):
        raise OrchestrationError("research pricing currency/provenance/cap invalid")
    max_calls = claims["max_model_calls"]
    bounded_tokens = max_calls * (
        claims["max_input_tokens_per_call"] +
        claims["max_output_tokens_per_call"])
    if bounded_tokens > claims["max_reported_tokens"]:
        raise OrchestrationError("research token envelope exceeds pinned governance")
    numerator = max_calls * (
        claims["max_input_tokens_per_call"] *
        claims["input_rate_micros_per_million"] +
        claims["max_output_tokens_per_call"] *
        claims["output_rate_micros_per_million"])
    worst_case_usd_micros = (numerator + 999_999) // 1_000_000
    if worst_case_usd_micros > claims["max_total_cost_usd_micros"]:
        raise OrchestrationError("research grant ceiling insufficient for worst-case tokens")
    return {
        "max_input_tokens_per_call": claims["max_input_tokens_per_call"],
        "max_output_tokens_per_call": claims["max_output_tokens_per_call"],
        "max_model_calls": max_calls,
        "worst_case_usd_micros": worst_case_usd_micros,
        "approved_usd_micros": claims["max_total_cost_usd_micros"],
        "currency": "USD",
    }


def verify_research_grant(orchestrator, task_id, model_config,
                          governance_policy, receipt, trust_policy, *, now=None):
    """Cryptographic audit only, NEVER independent production admission."""
    fixed = expected_research_consent_fields(
        orchestrator, task_id, model_config, governance_policy)
    if (not isinstance(receipt, dict)
            or set(receipt) != {"claims", "signature"}
            or not isinstance(receipt["claims"], dict)
            or set(receipt["claims"]) != _CLAIMS):
        raise OrchestrationError("research grant receipt schema invalid")
    claims = receipt["claims"]
    if any(claims.get(k) != v for k, v in fixed.items()):
        raise OrchestrationError("research grant not bound to prepared Task/role/scope")
    for field in ("grant_id", "operator_approval_id"):
        if not isinstance(claims[field], str) or not _HEX32.fullmatch(claims[field]):
            raise OrchestrationError("research grant or approval ID malformed")
    for field in ("issuer_id", "key_id"):
        if not isinstance(claims[field], str) or not _ISSUER_ID.fullmatch(claims[field]):
            raise OrchestrationError("research grant issuer identity malformed")
    clock = datetime.now(timezone.utc) if now is None else now
    if not isinstance(clock, datetime) or clock.tzinfo is None:
        raise OrchestrationError("research approval clock invalid")
    clock = clock.astimezone(timezone.utc)
    start, end = _utc(claims["issued_at"]), _utc(claims["expires_at"])
    if (start > clock or end <= clock or end <= start
            or end - start > _MAX_VALIDITY):
        raise OrchestrationError("research approval expired/premature/too long")
    envelope = _cost_envelope(claims)
    key = _trusted_public_key(trust_policy, claims)
    try:
        signature = base64.b64decode(receipt["signature"], validate=True)
        if len(signature) != 64:
            raise ValueError("wrong signature length")
        key.verify(signature, _CONTEXT + canonical_json_bytes(claims))
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise OrchestrationError("research grant signature invalid") from exc
    return {
        "schema_version": 1, "task_id": task_id,
        "status": "EXTERNAL_RESEARCH_SIGNATURE_VERIFIED_AUDIT_ONLY",
        "grant_id": claims["grant_id"],
        "operator_approval_id": claims["operator_approval_id"],
        "receipt_sha256": fingerprint_json(receipt),
        "price_evidence_sha256": claims["price_evidence_sha256"],
        "cost_envelope": envelope,
        "signature_verified": True,
        "trusted_issuer_independently_provisioned": False,
        "pricing_provenance_independently_verified": False,
        "single_use_grant_consumed": False,
        "permission_to_dispatch": False,
        "model_call_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "automatic_retry_allowed": False,
        "brain_invoked": False,
        "provider_invoked": False,
    }


def inspect_research_live_readiness(orchestrator, task_id, model_config,
                                    governance_policy, *, config_source,
                                    receipt=None, trust_policy=None):
    """Negative-only readiness report; model and BRAIN never instantiated."""
    fixed = expected_research_consent_fields(
        orchestrator, task_id, model_config, governance_policy)
    blockers = [
        "RESEARCH_ONLY_GRANT_NOT_INDEPENDENTLY_PROVISIONED",
        "RESEARCH_GRANT_SINGLE_USE_CONSUMER_NOT_IMPLEMENTED",
        "RESEARCH_MODEL_HARD_COST_ENFORCEMENT_NOT_IMPLEMENTED",
        "RESEARCH_MODEL_PRICING_SOURCE_NOT_ATTESTED",
        "RESEARCH_BRAIN_CATALOG_READ_PROFILE_NOT_ADMITTED",
        "RESEARCH_PAID_MODEL_ADAPTER_DISABLED",
        "RESEARCH_PLATFORM_SESSION_NOT_VERIFIED",
        "RESEARCH_READBACK_RECOVERY_NOT_VERIFIED",
    ]
    if config_source != "user_config":
        blockers.append("RESEARCH_OFFLINE_DEFAULTS_NOT_LIVE_CONFIG")
    grant = None
    if (receipt is None) != (trust_policy is None):
        raise OrchestrationError("research receipt and issuer trust policy must be paired")
    if receipt is not None:
        grant = verify_research_grant(
            orchestrator, task_id, model_config, governance_policy,
            receipt, trust_policy)
        # Signature verification does not attest that user-controlled trust
        # roots, remote pricing or single-use dispatch exist in production.
        blockers.append("RESEARCH_LOCAL_SIGNATURE_ONLY_NOT_INDEPENDENTLY_ATTESTED")
    return {
        "schema_version": 1, "task_id": task_id,
        "workflow": fixed["workflow"],
        "status": "RESEARCH_LIVE_BLOCKED", "audit_mode": "READ_ONLY",
        "scope": fixed["scope"], "max_model_calls": fixed["max_model_calls"],
        "max_reported_tokens": fixed["max_reported_tokens"],
        "grant_verification": grant,
        "blockers": sorted(set(blockers)),
        "live_research_authorized": False,
        "permission_to_dispatch": False,
        "model_call_authorized": False,
        "brain_read_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "automatic_retry_allowed": False,
        "brain_invoked": False, "provider_invoked": False,
    }
