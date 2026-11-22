"""P4.19 strict provider request/usage boundary, without a remote SDK call.

Compiles a fixed OpenAI-compatible Chat Completion request shape for the
registered research role and reuses the signed P4.17 cost preflight. This
module neither creates an OpenAI client nor reads API credentials. The
declared input token count, supplied tariff and post-call provider usage
require independent attestation and cannot authorize a paid operation.
"""
from alpha_research.agents.configuration import RoleRoutingConfig
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_model_cost_boundary import (
    inspect_model_request_cost,
)


def build_research_sdk_request_plan(
        orchestrator, task_id, model_config, governance_policy,
        receipt, trust_policy, *, task_name, declared_input_tokens,
        max_output_tokens, now=None):
    if task_name not in ("construct_super", "optimize_alpha"):
        raise OrchestrationError("research SDK task is not an approved planning task")
    workflow = orchestrator.load(task_id)["workflow"]
    if (workflow == "super" and task_name != "construct_super"
            or workflow == "optimization" and task_name != "optimize_alpha"):
        raise OrchestrationError("research SDK task differs from Task workflow")
    cost = inspect_model_request_cost(
        orchestrator, task_id, model_config, governance_policy,
        receipt, trust_policy, input_tokens=declared_input_tokens,
        max_output_tokens=max_output_tokens, now=now)
    binding = RoleRoutingConfig.parse(model_config).resolve(task_name)
    backend = binding.backend
    if (backend.get("provider") != "openai_sdk"
            or backend.get("kind") != "llm_api"
            or not isinstance(backend.get("model"), str)
            or not backend["model"].strip()):
        raise OrchestrationError(
            "research SDK adapter supports only registered OpenAI-compatible LLM backends")
    # Explicit allowlist. No functions/tools, arbitrary **kwargs, model
    # failover, streaming or unbounded completion length are accepted.
    options = {
        "model": backend["model"],
        "max_tokens": max_output_tokens,
        "stream": False,
        "n": 1,
    }
    return {
        "schema_version": 1,
        "task_id": task_id,
        "task_name": task_name,
        "role": binding.role,
        "backend_name": binding.backend_name,
        "provider": "openai_sdk",
        "status": "SDK_REQUEST_SHAPE_VALIDATED_NOT_EXECUTABLE",
        "fixed_sdk_options": options,
        "declared_input_tokens": declared_input_tokens,
        "receipt_sha256": cost["receipt_sha256"],
        "worst_case_request_usd_micros": cost["worst_case_request_usd_micros"],
        "currency": "USD",
        "provider_tokenizer_attested": False,
        "provider_hard_output_cap_attested": False,
        "provider_tariff_attested": False,
        "provider_usage_reconciled": False,
        "model_call_authorized": False,
        "brain_read_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "permission_to_dispatch": False,
    }


def inspect_research_sdk_usage(plan, usage):
    """Check reported SDK usage. Missing usage is never assumed free."""
    if (not isinstance(plan, dict)
            or plan.get("status") != "SDK_REQUEST_SHAPE_VALIDATED_NOT_EXECUTABLE"
            or plan.get("model_call_authorized") is not False
            or not isinstance(plan.get("fixed_sdk_options"), dict)
            or set(plan["fixed_sdk_options"]) != {"model", "max_tokens", "stream", "n"}):
        raise OrchestrationError("research SDK request plan invalid")
    base = {
        "schema_version": 1, "task_id": plan["task_id"],
        "permission_to_dispatch": False,
        "model_call_authorized": False,
        "brain_read_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "automatic_retry_allowed": False,
        "provider_usage_reconciled": False,
    }
    if (not isinstance(usage, dict)
            or set(usage) != {"prompt_tokens", "completion_tokens", "total_tokens"}
            or any(type(usage.get(k)) is not int or usage[k] < 0
                   for k in ("prompt_tokens", "completion_tokens", "total_tokens"))
            or usage["total_tokens"] != (
                usage["prompt_tokens"] + usage["completion_tokens"])):
        return {**base, "status": "PROVIDER_USAGE_UNKNOWN_RECONCILIATION_REQUIRED"}
    if (usage["prompt_tokens"] > plan["declared_input_tokens"]
            or usage["completion_tokens"] > plan["fixed_sdk_options"]["max_tokens"]):
        return {**base, "status": "PROVIDER_TOKEN_CEILING_VIOLATION"}
    return {
        **base,
        "status": "PROVIDER_REPORTED_USAGE_WITHIN_DECLARED_CAPS_AUDIT_ONLY",
        "reported_input_tokens": usage["prompt_tokens"],
        "reported_output_tokens": usage["completion_tokens"],
        "provider_usage_reconciled": False,
    }


def dispatch_paid_research_model(*_args, **_kwargs):
    raise OrchestrationError(
        "paid research SDK dispatch requires independently attested global grant and metering")
