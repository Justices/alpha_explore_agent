"""P4.28: offline verification of signed remote model-operation readback.

Unknown, missing or inconsistent remote outcomes must never be retried
automatically. No SDK, BRAIN session or recovery network call is created.
"""
import re

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_attestation_contract import (
    inspect_signed_research_evidence,
)
from alpha_research.state.fingerprint import fingerprint_json

_FIELDS = frozenset({
    "grant_ack_sha256", "model_attempt_id", "provider_operation_id",
    "remote_status", "prompt_tokens", "completion_tokens",
    "total_tokens", "charged_usd_micros", "readback_source",
})
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
_STATUSES = {"NOT_FOUND", "IN_FLIGHT", "SETTLED", "FAILED", "UNKNOWN"}


def inspect_research_remote_reconciliation(task_id, receipt, ack,
                                           model_attempt_id, readback, trust,
                                           *, max_input_tokens,
                                           max_output_tokens,
                                           max_total_cost_usd_micros, now=None):
    """Bind a signed readback to a specific non-idempotent model attempt."""
    if (not isinstance(model_attempt_id, str)
            or not _HEX32.fullmatch(model_attempt_id)
            or type(max_input_tokens) is not int or max_input_tokens < 1
            or type(max_output_tokens) is not int or max_output_tokens < 1
            or type(max_total_cost_usd_micros) is not int
            or max_total_cost_usd_micros < 1):
        raise OrchestrationError("remote reconciliation attempt or caps invalid")
    expected = {
        "task_id": task_id,
        "research_receipt_sha256": fingerprint_json(receipt),
        "grant_ack_sha256": fingerprint_json(ack),
        "model_attempt_id": model_attempt_id,
        "readback_source": "REMOTE_PROVIDER_READBACK",
    }
    signed = inspect_signed_research_evidence(
        readback, trust, domain="research-model-readback",
        operation="observe_research_model_outcome",
        fields=_FIELDS, expected=expected, now=now)
    claims = readback["claims"]
    if (claims["remote_status"] not in _STATUSES
            or not isinstance(claims["provider_operation_id"], str)
            or not _ID.fullmatch(claims["provider_operation_id"])
            or any(type(claims[k]) is not int or claims[k] < 0
                   for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                             "charged_usd_micros"))
            or claims["prompt_tokens"] + claims["completion_tokens"] != claims["total_tokens"]):
        raise OrchestrationError("remote readback provider outcome invalid")
    exceeded = (claims["prompt_tokens"] > max_input_tokens
                or claims["completion_tokens"] > max_output_tokens)
    if claims["charged_usd_micros"] > max_total_cost_usd_micros:
        status = "RECONCILIATION_REQUIRED_COST_CEILING_VIOLATION"
    elif exceeded:
        status = "RECONCILIATION_REQUIRED_TOKEN_CEILING_VIOLATION"
    elif claims["remote_status"] in {"NOT_FOUND", "IN_FLIGHT", "UNKNOWN"}:
        status = "RECONCILIATION_REQUIRED_REMOTE_OUTCOME_UNCERTAIN"
    elif claims["remote_status"] == "FAILED":
        status = "RECONCILIATION_REQUIRED_REMOTE_FAILURE"
    else:
        status = "REMOTE_SETTLED_SIGNATURE_VERIFIED_AUDIT_ONLY"
    return {
        **signed,
        "status": status,
        "remote_status": claims["remote_status"],
        "model_attempt_id": model_attempt_id,
        "reported_total_tokens": claims["total_tokens"],
        "reported_charge_usd_micros": claims["charged_usd_micros"],
        "provider_billing_independently_reconciled": False,
        "remote_outcome_independently_read": False,
        "safe_to_retry": False,
        "automatic_retry_allowed": False,
    }
