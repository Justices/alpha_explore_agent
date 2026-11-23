"""Pure provider endpoint, metering and signed readback audits.

This module cannot open a Provider connection or release budget reservations.
"""
from urllib.parse import urlsplit

from alpha_research.agents.configuration import RoleRoutingConfig
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_remote_reconciliation import (
    inspect_research_remote_reconciliation,
)
from alpha_research.orchestration.research_sdk_boundary import (
    build_research_sdk_request_plan, inspect_research_sdk_usage,
)
from alpha_research.orchestration.research_model_cost_boundary import ceil_cost_usd_micros
from alpha_research.state.fingerprint import fingerprint_json

# Host, path and credential identity form ONE provider-specific contract.
# A host from provider A cannot silently inherit provider B's API root.
_PROVIDER_ENDPOINTS = {
    "api.deepseek.com": {
        "paths": ("", "/", "/v1"),
        "key": "DEEPSEEK_API_KEY",
    },
    "open.bigmodel.cn": {
        "paths": ("/api/paas/v4",),
        "key": "ZAI_API_KEY",
    },
    "api.moonshot.cn": {
        "paths": ("/v1",),
        "key": "MOONSHOT_API_KEY",
    },
}


def inspect_p6_provider_plan(orchestrator, task_id, model_config,
                             governance_policy, receipt, operator_trust, *,
                             task_name, declared_input_tokens,
                             max_output_tokens, now=None):
    """Compile pinned registered SDK provider frame; no socket is constructed."""
    sdk = build_research_sdk_request_plan(
        orchestrator, task_id, model_config, governance_policy,
        receipt, operator_trust, task_name=task_name,
        declared_input_tokens=declared_input_tokens,
        max_output_tokens=max_output_tokens, now=now)
    binding = RoleRoutingConfig.parse(model_config).resolve(task_name)
    backend = binding.backend
    raw_url = backend.get("base_url")
    if not isinstance(raw_url, str):
        raise OrchestrationError("P6 registered backend HTTPS URL missing")
    try:
        parsed = urlsplit(raw_url)
        endpoint = _PROVIDER_ENDPOINTS.get(parsed.hostname)
        port = parsed.port
    except ValueError as exc:
        raise OrchestrationError("P6 provider HTTPS endpoint invalid") from exc
    if (parsed.scheme != "https" or endpoint is None
            or port not in (None, 443)
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or parsed.path not in endpoint["paths"]
            or backend.get("provider") != "openai_sdk"
            or backend.get("kind") != "llm_api"
            or backend.get("model") != sdk["fixed_sdk_options"]["model"]
            or backend.get("api_key_env") != endpoint["key"]):
        raise OrchestrationError("P6 provider is not a pinned registered HTTPS backend")
    return {
        "schema_version": 1,
        "task_id": task_id,
        "status": "P6_PROVIDER_REQUEST_CONTRACT_AUDITED_NO_LIVE",
        "task_name": task_name,
        "backend_name": binding.backend_name,
        "model": sdk["fixed_sdk_options"]["model"],
        "provider_hostname": parsed.hostname,
        "fixed_sdk_options_sha256": fingerprint_json(sdk["fixed_sdk_options"]),
        "receipt_sha256": sdk["receipt_sha256"],
        "declared_input_tokens": declared_input_tokens,
        "max_output_tokens": max_output_tokens,
        "worst_case_usd_micros": sdk["worst_case_request_usd_micros"],
        "max_retries": 0, "stream": False,
        "provider_tokenizer_independently_verified": False,
        "provider_output_limit_independently_enforced": False,
        "provider_credentials_loaded": False,
        "real_provider_invoked": False,
        "permission_to_dispatch": False,
    }


def inspect_p6_provider_metering(provider_plan, sdk_plan, usage, receipt,
                                  *, billed_usd_micros=None):
    """Integer-precise estimate; supplier-reported usage != verified invoice."""
    if (not isinstance(provider_plan, dict)
            or provider_plan.get("status")
               != "P6_PROVIDER_REQUEST_CONTRACT_AUDITED_NO_LIVE"
            or provider_plan.get("permission_to_dispatch") is not False
            or provider_plan.get("task_id") != sdk_plan.get("task_id")
            or provider_plan.get("receipt_sha256") != fingerprint_json(receipt)
            or provider_plan.get("fixed_sdk_options_sha256")
               != fingerprint_json(sdk_plan.get("fixed_sdk_options"))
            or provider_plan.get("worst_case_usd_micros")
               != sdk_plan.get("worst_case_request_usd_micros")
            or (billed_usd_micros is not None
                and (type(billed_usd_micros) is not int
                     or billed_usd_micros < 0))):
        raise OrchestrationError("P6 metering contract drift")
    audit = inspect_research_sdk_usage(sdk_plan, usage)
    upper = provider_plan["worst_case_usd_micros"]
    result = {
        "schema_version": 1, "task_id": provider_plan["task_id"],
        "status": audit["status"],
        "billing_estimate_usd_micros": None,
        "reported_invoice_usd_micros": billed_usd_micros,
        "estimated_invoice_variance_usd_micros": None,
        "worst_case_reservation_usd_micros": upper,
        "provider_invoice_independently_verified": False,
        "real_provider_invoked": False,
        "automatic_retry_allowed": False,
        "permission_to_dispatch": False,
    }
    if audit["status"] != "PROVIDER_REPORTED_USAGE_WITHIN_DECLARED_CAPS_AUDIT_ONLY":
        return {**result, "status": "P6_RECONCILIATION_REQUIRED_TOKEN_OR_USAGE"}
    claims = receipt.get("claims")
    if (not isinstance(claims, dict)
            or any(type(claims.get(k)) is not int or claims[k] < 0
                   for k in ("input_rate_micros_per_million",
                             "output_rate_micros_per_million"))):
        raise OrchestrationError("P6 signed price rates missing")
    estimate = ceil_cost_usd_micros(
        input_tokens=usage["prompt_tokens"],
        output_tokens=usage["completion_tokens"],
        input_rate_micros_per_million=claims[
            "input_rate_micros_per_million"],
        output_rate_micros_per_million=claims[
            "output_rate_micros_per_million"])
    variance = None if billed_usd_micros is None else billed_usd_micros - estimate
    status = "P6_REPORTED_USAGE_ESTIMATE_ONLY"
    if estimate > upper:
        status = "P6_RECONCILIATION_REQUIRED_RESERVATION_BREACH"
    elif billed_usd_micros is not None and (
            variance != 0 or billed_usd_micros > upper):
        status = "P6_RECONCILIATION_REQUIRED_INVOICE_VARIANCE"
    return {
        **result,
        "status": status,
        "billing_estimate_usd_micros": estimate,
        "estimated_invoice_variance_usd_micros": variance,
    }


def inspect_p6_signed_provider_readback(task_id, receipt, ack, attempt_id,
                                        evidence, trust, *, expected_operation_id,
                                        max_input_tokens, max_output_tokens,
                                        max_total_cost_usd_micros, now=None):
    """Inspect remotely signed outcome; cannot independently authenticate invoice."""
    report = inspect_research_remote_reconciliation(
        task_id, receipt, ack, attempt_id, evidence, trust,
        max_input_tokens=max_input_tokens,
        max_output_tokens=max_output_tokens,
        max_total_cost_usd_micros=max_total_cost_usd_micros, now=now)
    claims = evidence["claims"]
    if (not isinstance(expected_operation_id, str)
            or not expected_operation_id
            or expected_operation_id != claims["provider_operation_id"]):
        raise OrchestrationError("P6 provider operation ID differs from saved attempt")
    return {
        "schema_version": 1, "task_id": task_id,
        "status": "P6_SIGNED_READBACK_AUDITED_BILLING_UNVERIFIED",
        "remote_verdict": report["status"],
        "evidence_sha256": report["evidence_sha256"],
        "attempt_id": attempt_id,
        "remote_operation_id_matches_journal": True,
        "real_provider_invoice_verified": False,
        "server_to_provider_readback_executed": False,
        "safe_to_retry": False, "permission_to_dispatch": False,
    }


