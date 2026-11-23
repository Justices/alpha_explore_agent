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
from alpha_research.orchestration.research_model_cost_boundary import (
    ceil_cost_usd_micros,
)
from alpha_research.orchestration.research_p5_release import (
    inspect_research_p5_canary_gate,
)
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


# These are observations, not permissions. A failed audit must not be
# re-labelled as a completed milestone by the aggregate release gate.
_METERING_VERDICTS = frozenset({
    "P6_REPORTED_USAGE_ESTIMATE_ONLY",
    "P6_RECONCILIATION_REQUIRED_TOKEN_OR_USAGE",
    "P6_RECONCILIATION_REQUIRED_RESERVATION_BREACH",
    "P6_RECONCILIATION_REQUIRED_INVOICE_VARIANCE",
})


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
                or metering_report.get("real_provider_invoked") is not False
                or not isinstance(metering_report.get("status"), str)
                or metering_report["status"] not in _METERING_VERDICTS):
            raise OrchestrationError("P6 metering audit report invalid")
        if metering_report["status"] == "P6_REPORTED_USAGE_ESTIMATE_ONLY":
            statuses["p6_3_billing"] = "OFFLINE_ESTIMATE_ONLY_INVOICE_UNVERIFIED"
        else:
            statuses["p6_3_billing"] = "RECONCILIATION_REQUIRED"
    if brain_report is not None:
        if (not isinstance(brain_report, dict)
                or brain_report.get("task_id") != task_id
                or brain_report.get("status") not in (
                    "P6_READ_ONLY_FRAME_SHAPE_VERIFIED_NO_LIVE",
                    "P6_BRAIN_FRAME_DENIED")
                or brain_report.get("permission_to_dispatch") is not False
                or brain_report.get("brain_read_invoked") is not False
                or brain_report.get("brain_session_authenticated") is not False
                or brain_report.get("read_only_frame_shape_valid") is not (
                    brain_report["status"] == "P6_READ_ONLY_FRAME_SHAPE_VERIFIED_NO_LIVE")):
            raise OrchestrationError("P6 BRAIN read report invalid")
        statuses["p6_4_brain_read"] = (
            "OFFLINE_FRAME_POLICY_VERIFIED"
            if brain_report["read_only_frame_shape_valid"]
            else "READ_ONLY_FRAME_DENIED")
    if signed_readback is not None:
        if (not isinstance(signed_readback, dict)
                or signed_readback.get("task_id") != task_id
                or signed_readback.get("permission_to_dispatch") is not False
                or signed_readback.get("status")
                   != "P6_SIGNED_READBACK_AUDITED_BILLING_UNVERIFIED"
                or signed_readback.get("real_provider_invoice_verified") is not False
                or signed_readback.get("server_to_provider_readback_executed") is not False
                or signed_readback.get("remote_operation_id_matches_journal") is not True
                or signed_readback.get("safe_to_retry") is not False):
            raise OrchestrationError("P6 signed readback report invalid")
        # A signed operation report cannot clear a previously observed
        # token, reservation or invoice discrepancy.
        if statuses["p6_3_billing"] != "RECONCILIATION_REQUIRED":
            statuses["p6_3_billing"] = "SIGNED_OBSERVATION_ONLY_INVOICE_UNVERIFIED"
    blockers = set(baseline["blockers"]) | {
        "P6_REAL_MTLS_DEPLOYMENT_NOT_PROVEN",
        "P6_LIVE_PROVIDER_DISPATCH_DISABLED",
        "P6_INDEPENDENT_PROVIDER_INVOICE_NOT_VERIFIED",
        "P6_LIVE_BRAIN_READ_NOT_AUTHORIZED",
        "P6_LIVE_CANARY_AND_OPERATOR_APPROVAL_MISSING",
        "P6_SEPARATE_SIMULATION_SUBMISSION_APPROVAL_REQUIRED",
    }
    if statuses["p6_3_billing"] == "RECONCILIATION_REQUIRED":
        blockers.add("P6_METERING_RECONCILIATION_REQUIRED")
    if statuses["p6_4_brain_read"] == "READ_ONLY_FRAME_DENIED":
        blockers.add("P6_BRAIN_READ_FRAME_DENIED")
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



def run_p6_offline_canary(*, sdk_inputs, attempt_id, brain_read_frame):
    """P6 E2E research canary rehearsal with the real installed SDK mock.

    The only SDK transport on this path is internally constructed
    MockTransport from P5.3. The BRAIN frame is validated but NOT executed.
    Authority budget is reserved and remains held after the synthetic reply.
    """
    from alpha_research.orchestration.research_sdk_mock_pilot import (
        run_research_sdk_offline_pilot,
    )
    keys = {
        "orchestrator", "task_id", "model_config", "governance_policy",
        "receipt", "operator_trust", "ack", "consumer_trust",
        "ledger_evidence", "ledger_trust", "price_evidence",
        "price_trust", "authority_client", "task_name",
        "declared_input_tokens", "max_output_tokens", "messages",
        "fixture", "now",
    }
    if not isinstance(sdk_inputs, dict) or set(sdk_inputs) != keys:
        raise OrchestrationError("P6 Canary requires exact preapproved SDK mock inputs")
    inp = sdk_inputs
    brain = inspect_p6_brain_catalog_frame(
        inp["orchestrator"], inp["task_id"], brain_read_frame)
    if not brain["read_only_frame_shape_valid"]:
        raise OrchestrationError("P6 Canary BRAIN scope not a read-only frame")
    provider = inspect_p6_provider_plan(
        inp["orchestrator"], inp["task_id"], inp["model_config"],
        inp["governance_policy"], inp["receipt"], inp["operator_trust"],
        task_name=inp["task_name"],
        declared_input_tokens=inp["declared_input_tokens"],
        max_output_tokens=inp["max_output_tokens"], now=inp["now"])
    sdk_plan = build_research_sdk_request_plan(
        inp["orchestrator"], inp["task_id"], inp["model_config"],
        inp["governance_policy"], inp["receipt"], inp["operator_trust"],
        task_name=inp["task_name"],
        declared_input_tokens=inp["declared_input_tokens"],
        max_output_tokens=inp["max_output_tokens"], now=inp["now"])
    report = run_research_sdk_offline_pilot(
        **inp, attempt_id=attempt_id)
    if (report.get("status") != "SDK_MOCK_COMPLETED_COST_HELD_AUDIT_ONLY"
            or report.get("sdk_transport") != "IN_PROCESS_MOCK_ONLY"
            or report.get("sdk_max_retries") != 0
            or report.get("provider_invoked") is not False
            or report.get("permission_to_dispatch") is not False):
        raise OrchestrationError(
            "P6 mock outcome cannot establish execution authority")
    usage = {
        "prompt_tokens": report["reported_input_tokens"],
        "completion_tokens": report["reported_output_tokens"],
        "total_tokens": (
            report["reported_input_tokens"] + report["reported_output_tokens"]),
    }
    meter = inspect_p6_provider_metering(
        provider, sdk_plan, usage, inp["receipt"])
    telemetry = inp["authority_client"].canary_monitor()
    final = inspect_p6_integrated_release(
        inp["orchestrator"], inp["task_id"],
        authority_monitor=telemetry,
        provider_report=provider, metering_report=meter,
        brain_report=brain)
    if (final["status"] != "P6_PRODUCTION_BLOCKED"
            or final["permission_to_dispatch"] is not False):
        raise OrchestrationError("P6 Canary release boundary not blocked")
    return {
        "schema_version": 1, "task_id": inp["task_id"],
        "attempt_id": attempt_id,
        "status": "P6_OFFLINE_CANARY_REHEARSAL_COMPLETED",
        "model_request_mocked": True,
        "provider_api_invoked": False,
        "brain_api_invoked": False,
        "sdk_http_requests": report["sdk_http_requests"],
        "sdk_max_retries": 0,
        "sdk_response_sha256": report["response_content_sha256"],
        "provider_operation_id": report["provider_operation_id"],
        "declared_reserved_usd_micros": report["reserved_usd_micros"],
        "estimated_cost_usd_micros": meter["billing_estimate_usd_micros"],
        "invoice_independently_verified": False,
        "brain_frame_policy_valid": brain["read_only_frame_shape_valid"],
        "release_status": final["status"],
        "production_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "automatic_retry_allowed": False,
        "permission_to_dispatch": False,
    }
