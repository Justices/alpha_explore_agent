"""P4.18 composed read-only research runtime plan.

Bind the P4.16 signed research grant, P4.17 local audit claim, exact
Catalog scope, and model per-call cost envelope to the SAME Task/receipt.
This plan never yields an execution token or a live Gateway/SDK instance.
Even if every local check succeeds, the independent global consumer,
trusted price attestation, SDK enforcement and recovery are unimplemented.
"""
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_catalog_protocol import (
    inspect_catalog_read_contract,
)
from alpha_research.orchestration.research_grant_audit import (
    inspect_research_grant_claim,
)
from alpha_research.orchestration.research_model_cost_boundary import (
    inspect_model_request_cost,
)
from alpha_research.orchestration.research_only_admission import (
    verify_research_grant,
)

_EXTERNAL_BLOCKERS = (
    "RESEARCH_EXTERNAL_GRANT_CONSUMER_UNAVAILABLE",
    "RESEARCH_CATALOG_DRIVER_LIVE_ADMISSION_DISABLED",
    "RESEARCH_MODEL_SDK_HARD_CAP_NOT_ATTESTED",
    "RESEARCH_PRICE_PROVENANCE_NOT_ATTESTED",
    "RESEARCH_REMOTE_OUTCOME_READBACK_UNAVAILABLE",
    "RESEARCH_LIVE_CANARY_NOT_AUTHORIZED",
)


def inspect_research_runtime_plan(orchestrator, task_id, model_config,
                                  governance_policy, *, receipt=None,
                                  trust_policy=None,
                                  input_tokens=None, max_output_tokens=None,
                                  now=None):
    """Read-only composite readiness; full local receipt binding, always NO LIVE."""
    task = orchestrator.load(task_id)
    catalog = inspect_catalog_read_contract(orchestrator, task_id)
    local_claim = inspect_research_grant_claim(orchestrator, task_id)
    blockers = list(_EXTERNAL_BLOCKERS)
    signed = None
    model_request = None
    if (receipt is None) != (trust_policy is None):
        raise OrchestrationError("research receipt and trust must be supplied together")
    if receipt is None:
        if input_tokens is not None or max_output_tokens is not None:
            raise OrchestrationError("model request limits require a signed research receipt")
        blockers.append("RESEARCH_OPERATOR_SIGNATURE_MISSING")
    else:
        signed = verify_research_grant(
            orchestrator, task_id, model_config, governance_policy,
            receipt, trust_policy, now=now)
        if input_tokens is None or max_output_tokens is None:
            blockers.append("RESEARCH_MODEL_REQUEST_LIMITS_NOT_PROVIDED")
        else:
            model_request = inspect_model_request_cost(
                orchestrator, task_id, model_config, governance_policy,
                receipt, trust_policy, input_tokens=input_tokens,
                max_output_tokens=max_output_tokens, now=now)
            if model_request["receipt_sha256"] != signed["receipt_sha256"]:
                raise OrchestrationError("model price and signed receipt binding drift")
    if local_claim["status"] != "RESERVED_RESEARCH_GRANT_AUDIT_ONLY":
        blockers.append("RESEARCH_GRANT_NOT_LOCALLY_AUDITED")
        if local_claim["status"] == "RECONCILIATION_REQUIRED":
            blockers.append("RESEARCH_LOCAL_GRANT_RECONCILIATION_REQUIRED")
    elif signed is not None:
        if (local_claim["grant_id"] != signed["grant_id"]
                or local_claim["operator_approval_id"] != signed["operator_approval_id"]
                or local_claim["receipt_sha256"] != signed["receipt_sha256"]
                or local_claim["price_evidence_sha256"] != signed["price_evidence_sha256"]):
            raise OrchestrationError(
                "research runtime receipt differs from reserved local audit claim")
    else:
        blockers.append("RESEARCH_RESERVED_GRANT_RECEIPT_NOT_SUPPLIED")
    if catalog["task_id"] != task_id or task["status"] != "CREATED":
        raise OrchestrationError("research runtime task/scope drift")
    return {
        "schema_version": 1,
        "task_id": task_id, "workflow": task["workflow"],
        "status": "RESEARCH_RUNTIME_LIVE_BLOCKED",
        "audit_mode": "READ_ONLY",
        "catalog": catalog,
        "local_claim": local_claim,
        "signed_receipt": signed,
        "model_request_cost": model_request,
        "blockers": sorted(set(blockers)),
        "external_consumer_attested": False,
        "model_hard_cost_enforced": False,
        "platform_session_attested": False,
        "live_research_authorized": False,
        "permission_to_dispatch": False,
        "model_call_authorized": False,
        "brain_read_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "automatic_retry_allowed": False,
        "provider_invoked": False,
        "brain_invoked": False,
    }
