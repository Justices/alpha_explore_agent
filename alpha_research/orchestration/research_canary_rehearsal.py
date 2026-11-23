"""Isolated offline SDK mock and BRAIN frame canary rehearsal.

Only in-process MockTransport; no Provider/BRAIN network IO and no Alpha POST.
"""
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_provider_audit import (
    inspect_p6_provider_plan, inspect_p6_provider_metering,
)
from alpha_research.orchestration.research_brain_read_audit import (
    inspect_p6_brain_catalog_frame,
)
from alpha_research.orchestration.research_readiness_matrix import (
    inspect_p6_integrated_release,
)
from alpha_research.orchestration.research_sdk_boundary import (
    build_research_sdk_request_plan,
)

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
