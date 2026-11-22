"""P4.25-29 domain-separated, read-only external attestation verification.

A signed statement can be inspected but NEVER becomes dispatch authority.
Trust files supplied by this process cannot attest independent key custody,
a remote service invocation or a platform/billing reconciliation.
"""
import base64
import re
from datetime import datetime, timedelta, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_only_admission import _utc
from alpha_research.state.fingerprint import canonical_json_bytes, fingerprint_json

_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\\Z")
_HEX32 = re.compile(r"[0-9a-f]{32}\\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\\Z")
_COMMON = frozenset({
    "schema_version", "operation", "evidence_id", "task_id",
    "research_receipt_sha256", "issuer_id", "key_id",
    "issued_at", "expires_at",
})
_TRUST = {"schema_version", "issuers", "revoked_evidence"}


def inspect_signed_research_evidence(evidence, trust, *, domain, operation,
                                     fields, expected, now=None):
    """Validate an exact signed schema and immutable, typed claim binding.

    Each caller selects its own domain and operation to prevent signatures
    from one trust purpose from being replayed for another purpose.
    All results remain explicitly non-executable.
    """
    if (not isinstance(domain, str) or not _ID.fullmatch(domain)
            or not isinstance(operation, str) or not _ID.fullmatch(operation)
            or not isinstance(fields, (set, frozenset))
            or not isinstance(expected, dict)
            or not set(expected).issubset(_COMMON | fields)
            or not isinstance(evidence, dict)
            or set(evidence) != {"claims", "signature"}
            or not isinstance(evidence["claims"], dict)
            or set(evidence["claims"]) != _COMMON | fields):
        raise OrchestrationError("external research evidence schema invalid")
    claims = evidence["claims"]
    if (type(claims["schema_version"]) is not int
            or claims["schema_version"] != 1
            or claims["operation"] != operation
            or not isinstance(claims["evidence_id"], str)
            or not _HEX32.fullmatch(claims["evidence_id"])
            or not isinstance(claims["task_id"], str)
            or not _HEX32.fullmatch(claims["task_id"])
            or not isinstance(claims["research_receipt_sha256"], str)
            or not _HEX64.fullmatch(claims["research_receipt_sha256"])):
        raise OrchestrationError("external research evidence identity invalid")
    if any(type(claims[k]) is not type(v) or claims[k] != v
           for k, v in expected.items()):
        raise OrchestrationError("external research evidence Task or receipt binding drift")
    for name in ("issuer_id", "key_id"):
        if not isinstance(claims[name], str) or not _ID.fullmatch(claims[name]):
            raise OrchestrationError("external research evidence issuer invalid")
    clock = datetime.now(timezone.utc) if now is None else now
    if not isinstance(clock, datetime) or clock.tzinfo is None:
        raise OrchestrationError("external research evidence clock invalid")
    clock = clock.astimezone(timezone.utc)
    start, end = _utc(claims["issued_at"]), _utc(claims["expires_at"])
    if start > clock or end <= clock or end <= start or end - start > timedelta(minutes=5):
        raise OrchestrationError("external research evidence expired or future")
    if (not isinstance(trust, dict) or set(trust) != _TRUST
            or type(trust["schema_version"]) is not int
            or trust["schema_version"] != 1
            or not isinstance(trust["issuers"], dict)
            or not isinstance(trust["revoked_evidence"], list)
            or any(not isinstance(i, str) or not _HEX32.fullmatch(i)
                   for i in trust["revoked_evidence"])
            or len(trust["revoked_evidence"]) != len(set(trust["revoked_evidence"]))):
        raise OrchestrationError("external research evidence trust invalid")
    if claims["evidence_id"] in trust["revoked_evidence"]:
        raise OrchestrationError("external research evidence revoked")
    entry = trust["issuers"].get(claims["issuer_id"])
    if (not isinstance(entry, dict)
            or set(entry) != {"key_id", "public_key_b64", "enabled", "operations"}
            or entry["key_id"] != claims["key_id"]
            or entry["enabled"] is not True
            or entry["operations"] != [operation]
            or not isinstance(entry["public_key_b64"], str)):
        raise OrchestrationError("external research evidence issuer not trusted")
    try:
        key = base64.b64decode(entry["public_key_b64"], validate=True)
        signature = base64.b64decode(evidence["signature"], validate=True)
        if len(key) != 32 or len(signature) != 64:
            raise ValueError("invalid Ed25519 key or signature length")
        Ed25519PublicKey.from_public_bytes(key).verify(
            signature, ("alpha_research/" + domain + "/v1:").encode("ascii")
            + canonical_json_bytes(claims))
    except (ValueError, TypeError, InvalidSignature) as exc:
        raise OrchestrationError("external research evidence signature invalid") from exc
    return {
        "schema_version": 1,
        "task_id": claims["task_id"],
        "evidence_id": claims["evidence_id"],
        "evidence_sha256": fingerprint_json(evidence),
        "status": "EXTERNAL_SIGNATURE_VERIFIED_AUDIT_ONLY",
        "independent_key_custody_attested": False,
        "remote_readback_attested": False,
        "permission_to_dispatch": False,
        "model_call_authorized": False,
        "brain_read_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "automatic_retry_allowed": False,
    }
