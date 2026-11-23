"""P6.2-P6.6 composed offline readiness and hard release blocker contracts.

These inspections correlate signed Task/Grant and configured role/model/price
metadata. They cannot prove independent provider billing, mTLS operations,
BRAIN authenticated sessions or operator permission to launch production.
No credentials, SDK calls, network calls, filesystem mutation or retries.
"""
from urllib.parse import urlsplit

from alpha_research.agents.configuration import RoleRoutingConfig
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_catalog_protocol import (
    inspect_catalog_read_contract, research_catalog_request_allowed,
)
from alpha_research.orchestration.research_remote_reconciliation import (
    inspect_research_remote_reconciliation,
)
from alpha_research.orchestration.research_sdk_boundary import (
    build_research_sdk_request_plan, inspect_research_sdk_usage,
)
from alpha_research.orchestration.research_p5_release import (
    inspect_research_p5_canary_gate,
)
from alpha_research.state.fingerprint import fingerprint_json

_ALLOWED_HOSTS = frozenset({
    "api.deepseek.com", "open.bigmodel.cn", "api.moonshot.cn",
})
_TASKS = frozenset({"construct_super", "optimize_alpha"})


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
    parsed = urlsplit(raw_url)
    if (parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS
            or parsed.port not in (None, 443)
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
            or parsed.path not in ("", "/", "/v1", "/api/paas/v4")
            or backend.get("provider") != "openai_sdk"
            or backend.get("kind") != "llm_api"
            or backend.get("model") != sdk["fixed_sdk_options"]["model"]
            or not isinstance(backend.get("api_key_env"), str)
            or not backend["api_key_env"].endswith("_API_KEY")):
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
    numerator = (
        usage["prompt_tokens"] * claims["input_rate_micros_per_million"]
        + usage["completion_tokens"] * claims["output_rate_micros_per_million"])
    estimate = (numerator + 999_999) // 1_000_000
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


def inspect_p6_brain_catalog_frame(orchestrator, task_id, frame):
    """P6.4 real BRAIN operation allowlist is compiled but cannot dispatch."""
    contract = inspect_catalog_read_contract(orchestrator, task_id)
    if not isinstance(frame, dict) or type(frame.get("cmd")) is not str:
        raise OrchestrationError("P6 BRAIN frame invalid")
    allowed = (
        frame["cmd"] in contract["allowed_read_actions"]
        and research_catalog_request_allowed(
            frame["cmd"], {k: v for k, v in frame.items() if k != "cmd"},
            contract["scope"],
            source_alpha_id=orchestrator.load(task_id)["request"].get("alpha_id")))
    return {
        "schema_version": 1, "task_id": task_id,
        "status": ("P6_READ_ONLY_FRAME_SHAPE_VERIFIED_NO_LIVE"
                   if allowed else "P6_BRAIN_FRAME_DENIED"),
        "read_only_frame_shape_valid": allowed,
        "scope": contract["scope"],
        "credential_provider_initialized": False,
        "brain_session_authenticated": False,
        "brain_read_invoked": False,
        "brain_simulation_invoked": False,
        "alpha_submission_invoked": False,
        "permission_to_dispatch": False,
    }


def inspect_p6_integrated_release(orchestrator, task_id, *,
                                  authority_monitor=None,
                                  provider_report=None, metering_report=None,
                                  brain_report=None, signed_readback=None):
    """P6.5/6.6 dry-run readiness matrix remains strictly non-executable."""
    baseline = inspect_research_p5_canary_gate(
        orchestrator, task_id, authority_monitor=authority_monitor)
    statuses = {
        "p6_1_service_identity": "LOCAL_MTLS_NOT_PRODUCTION_ATTESTED",
        "p6_2_provider": "NOT_VERIFIED",
        "p6_3_billing": "NOT_VERIFIED",
        "p6_4_brain_read": "NOT_VERIFIED",
        "p6_5_canary": "NOT_APPROVED",
        "p6_6_release": "PRODUCTION_BLOCKED",
    }
    if provider_report is not None:
        if (not isinstance(provider_report, dict)
                or provider_report.get("task_id") != task_id
                or provider_report.get("status")
                   != "P6_PROVIDER_REQUEST_CONTRACT_AUDITED_NO_LIVE"
                or provider_report.get("permission_to_dispatch") is not False
                or provider_report.get("real_provider_invoked") is not False):
            raise OrchestrationError("P6 provider audit report invalid")
        statuses["p6_2_provider"] = "OFFLINE_REQUEST_CONTRACT_VERIFIED"
    if metering_report is not None:
        if (not isinstance(metering_report, dict)
                or metering_report.get("task_id") != task_id
                or metering_report.get("permission_to_dispatch") is not False
                or metering_report.get("provider_invoice_independently_verified")
                   is not False
                or not isinstance(metering_report.get("status"), str)
                or not metering_report["status"].startswith("P6_")):
            raise OrchestrationError("P6 metering audit report invalid")
        statuses["p6_3_billing"] = "OFFLINE_METERING_AUDITED"
    if brain_report is not None:
        if (not isinstance(brain_report, dict)
                or brain_report.get("task_id") != task_id
                or brain_report.get("status") not in (
                    "P6_READ_ONLY_FRAME_SHAPE_VERIFIED_NO_LIVE",
                    "P6_BRAIN_FRAME_DENIED")
                or brain_report.get("permission_to_dispatch") is not False
                or brain_report.get("brain_read_invoked") is not False):
            raise OrchestrationError("P6 BRAIN read report invalid")
        statuses["p6_4_brain_read"] = "OFFLINE_FRAME_POLICY_VERIFIED"
    if signed_readback is not None:
        if (not isinstance(signed_readback, dict)
                or signed_readback.get("task_id") != task_id
                or signed_readback.get("permission_to_dispatch") is not False
                or signed_readback.get("status")
                   != "P6_SIGNED_READBACK_AUDITED_BILLING_UNVERIFIED"):
            raise OrchestrationError("P6 signed readback report invalid")
        statuses["p6_3_billing"] = "SIGNED_OBSERVATION_ONLY_INVOICE_UNVERIFIED"
    blockers = set(baseline["blockers"]) | {
        "P6_REAL_MTLS_DEPLOYMENT_NOT_PROVEN",
        "P6_LIVE_PROVIDER_DISPATCH_DISABLED",
        "P6_INDEPENDENT_PROVIDER_INVOICE_NOT_VERIFIED",
        "P6_LIVE_BRAIN_READ_NOT_AUTHORIZED",
        "P6_LIVE_CANARY_AND_OPERATOR_APPROVAL_MISSING",
        "P6_SEPARATE_SIMULATION_SUBMISSION_APPROVAL_REQUIRED",
    }
    return {
        "schema_version": 1, "task_id": task_id,
        "workflow": baseline["workflow"],
        "status": "P6_PRODUCTION_BLOCKED",
        "audit_mode": "READ_ONLY",
        "milestones": statuses,
        "blockers": sorted(blockers),
        "canary_rehearsal_only": True,
        "external_service_deployed": False,
        "provider_invoice_independently_verified": False,
        "production_authorized": False,
        "brain_read_authorized": False,
        "model_call_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "automatic_retry_allowed": False,
        "permission_to_dispatch": False,
        "brain_invoked": False, "provider_invoked": False,
    }
