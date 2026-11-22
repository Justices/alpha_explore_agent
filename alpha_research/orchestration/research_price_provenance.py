"""P4.27: model-specific signed tariff provenance and hard-limit audit.

Tariff evidence and the declared SDK token envelope are immutable inputs to
a future global spend reservation. Neither is provider billing proof.
"""
import re

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_attestation_contract import (
    inspect_signed_research_evidence,
)
from alpha_research.orchestration.research_sdk_boundary import (
    build_research_sdk_request_plan, inspect_research_sdk_usage,
)
from alpha_research.state.fingerprint import fingerprint_json

_FIELDS = frozenset({
    "tariff", "tariff_sha256", "backend_model", "model_profile_sha256",
    "sdk_request_sha256", "price_status",
})
_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,160}\Z")
_TARIFF_KEYS = {
    "currency", "input_rate_micros_per_million",
    "output_rate_micros_per_million", "billing_unit",
}


def inspect_research_price_provenance(
        orchestrator, task_id, model_config, governance_policy,
        receipt, operator_trust, price_evidence, price_trust, *,
        task_name, input_tokens, max_output_tokens, now=None):
    """Tie a tariff signature to the exact model and approved cost envelope."""
    sdk = build_research_sdk_request_plan(
        orchestrator, task_id, model_config, governance_policy,
        receipt, operator_trust, task_name=task_name,
        declared_input_tokens=input_tokens,
        max_output_tokens=max_output_tokens, now=now)
    claims = receipt["claims"]
    tariff = {
        "currency": "USD",
        "input_rate_micros_per_million": claims["input_rate_micros_per_million"],
        "output_rate_micros_per_million": claims["output_rate_micros_per_million"],
        "billing_unit": "TOKENS_PER_MILLION",
    }
    expected = {
        "task_id": task_id,
        "research_receipt_sha256": sdk["receipt_sha256"],
        "tariff": tariff,
        "tariff_sha256": claims["price_evidence_sha256"],
        "backend_model": sdk["fixed_sdk_options"]["model"],
        "model_profile_sha256": claims["model_profile_sha256"],
        "sdk_request_sha256": fingerprint_json(sdk["fixed_sdk_options"]),
        "price_status": "PUBLISHED_TARIFF",
    }
    if (not isinstance(price_evidence, dict)
            or not isinstance(price_evidence.get("claims"), dict)
            or not isinstance(price_evidence["claims"].get("tariff"), dict)
            or set(price_evidence["claims"]["tariff"]) != _TARIFF_KEYS
            or fingerprint_json(tariff) != claims["price_evidence_sha256"]):
        raise OrchestrationError(
            "research model tariff digest does not match signed grant pricing")
    if (not isinstance(expected["backend_model"], str)
            or not _ID.fullmatch(expected["backend_model"])):
        raise OrchestrationError("research model tariff backend invalid")
    result = inspect_signed_research_evidence(
        price_evidence, price_trust, domain="research-model-price",
        operation="observe_research_model_tariff", fields=_FIELDS,
        expected=expected, now=now)
    return {
        **result,
        "status": "MODEL_TARIFF_SIGNATURE_VERIFIED_AUDIT_ONLY",
        "sdk_request_sha256": expected["sdk_request_sha256"],
        "tariff_sha256": expected["tariff_sha256"],
        "worst_case_request_usd_micros": sdk["worst_case_request_usd_micros"],
        "provider_pricing_independently_attested": False,
        "global_spend_reserved": False,
        "sdk_output_hard_cap_enforced": False,
        "provider_usage_reconciled": False,
    }


def inspect_untrusted_provider_metering(plan, usage):
    """Preserve uncertainty; a declared token count cannot settle billing."""
    report = inspect_research_sdk_usage(plan, usage)
    return {
        **report,
        "billing_reconciled": False,
        "global_spend_reserved": False,
        "automatic_retry_allowed": False,
        "permission_to_dispatch": False,
    }
