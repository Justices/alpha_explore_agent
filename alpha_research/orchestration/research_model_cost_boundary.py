"""P4.17 model request *preflight*, with no paid-provider dispatch.

The signed worst-case envelope is checked AGAIN for each proposed model
attempt using strict integer arithmetic. This cannot establish token counts
actually billed by the provider; the real adapter must independently enforce
max_output_tokens, account for tokenization and reserved spend, and verify
provider invoices before a live mode could be enabled.
"""
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_only_admission import (
    verify_research_grant,
)


def inspect_model_request_cost(orchestrator, task_id, model_config,
                               governance_policy, receipt, trust_policy, *,
                               input_tokens, max_output_tokens, now=None):
    verified = verify_research_grant(
        orchestrator, task_id, model_config, governance_policy,
        receipt, trust_policy, now=now)
    claims = receipt["claims"]
    if (type(input_tokens) is not int or input_tokens < 1
            or type(max_output_tokens) is not int or max_output_tokens < 1):
        raise OrchestrationError("model request requires positive integer token ceilings")
    if (input_tokens > claims["max_input_tokens_per_call"]
            or max_output_tokens > claims["max_output_tokens_per_call"]):
        raise OrchestrationError("model request exceeds signed per-call token ceiling")
    if input_tokens + max_output_tokens > claims["max_reported_tokens"]:
        raise OrchestrationError("model request exceeds total signed token budget")
    numerator = (
        input_tokens * claims["input_rate_micros_per_million"]
        + max_output_tokens * claims["output_rate_micros_per_million"])
    upper = (numerator + 999_999) // 1_000_000
    if upper > claims["max_total_cost_usd_micros"]:
        raise OrchestrationError("model request exceeds signed USD ceiling")
    # Research grant says at most N calls, but never infer that the caller
    # has genuinely reserved a call or that another host did not consume it.
    return {
        "schema_version": 1, "task_id": task_id,
        "status": "MODEL_REQUEST_ENVELOPE_VALIDATED_NOT_EXECUTABLE",
        "receipt_sha256": verified["receipt_sha256"],
        "input_token_limit": input_tokens,
        "requested_output_token_limit": max_output_tokens,
        "worst_case_request_usd_micros": upper,
        "signed_total_cost_usd_micros": claims["max_total_cost_usd_micros"],
        "currency": "USD",
        "provider_tokenizer_attested": False,
        "provider_output_hard_limit_attested": False,
        "remaining_global_spend_attested": False,
        "provider_pricing_attested": False,
        "permission_to_dispatch": False,
        "model_call_authorized": False,
        "brain_read_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "automatic_retry_allowed": False,
        "provider_invoked": False,
        "brain_invoked": False,
    }
