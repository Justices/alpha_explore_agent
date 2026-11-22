"""Single read-only status for the full research preparation control plane.

A deliberately small projection replaces the need to inspect a dozen CLI
interfaces just to learn what is blocked. It never manufactures a Manifest,
issuer grant, provider session, model invocation, or BRAIN dispatch.
"""
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.manifest_builder import (
    inspect_generated_manifest,
)
from alpha_research.orchestration.research_preparation import (
    inspect_research_preparation,
)
from alpha_research.orchestration.research_runtime_adapter import (
    inspect_runtime_attempt,
)
from alpha_research.orchestration.research_grant_audit import (
    inspect_research_grant_claim,
)
from alpha_research.orchestration.research_catalog_protocol import (
    inspect_catalog_read_contract,
)
from alpha_research.orchestration.research_runtime_plan import (
    inspect_research_runtime_plan,
)

_COMPONENTS = ("research_preparation", "model_attempt", "manifest", "grant_audit",
               "catalog_contract", "live_plan")


def _status(name, callback):
    try:
        record = callback()
        if not isinstance(record, dict) or not isinstance(record.get("status"), str):
            raise OrchestrationError("research status component did not return a status")
        return record["status"]
    except (OrchestrationError, OSError, ValueError, TypeError, KeyError):
        # Reporting must never "fix" evidence or silently reset a failed
        # journal. Keep the source untouched for manual investigation.
        return "RECONCILIATION_REQUIRED"


def inspect_research_control_plane(orchestrator, task_id, model_config,
                                   governance_policy):
    """One status command for an existing task, no added files or network."""
    task = orchestrator.load(task_id)
    if task.get("workflow") not in {"super", "optimization"}:
        raise OrchestrationError(
            "research control-plane status requires Super/Optimization Task")
    request = task["request"]
    statuses = {
        "research_preparation": _status(
            "preparation", lambda: inspect_research_preparation(
                orchestrator, task_id)),
        "model_attempt": _status(
            "model_attempt", lambda: inspect_runtime_attempt(
                orchestrator, task_id)),
        "manifest": _status(
            "manifest", lambda: inspect_generated_manifest(
                orchestrator, task_id)),
        "grant_audit": _status(
            "grant", lambda: inspect_research_grant_claim(
                orchestrator, task_id)),
        "catalog_contract": _status(
            "catalog", lambda: inspect_catalog_read_contract(
                orchestrator, task_id)),
        "live_plan": _status(
            "plan", lambda: inspect_research_runtime_plan(
                orchestrator, task_id, model_config, governance_policy)),
    }
    if set(statuses) != set(_COMPONENTS):
        raise OrchestrationError("research status component list drift")
    if "RECONCILIATION_REQUIRED" in statuses.values():
        state = "RECONCILIATION_REQUIRED"
        next_required = "MANUAL_STATE_AND_EVIDENCE_RECONCILIATION"
    elif statuses["manifest"] == "GENERATED_LOCAL_NOT_AUTHORIZED":
        state = "AWAITING_INDEPENDENT_RESEARCH_AUTHORIZATION"
        next_required = "INDEPENDENT_RESEARCH_ISSUER_AND_GLOBAL_CONSUMER"
    else:
        state = "RESEARCH_OUTPUT_NOT_READY_LIVE_BLOCKED"
        next_required = "AUTHORIZED_RESEARCH_AGENT_CANDIDATE_GENERATION"
    return {
        "schema_version": 1,
        "task_id": task_id,
        "workflow": task["workflow"],
        "status": state,
        "task_status": task["status"],
        "scope": {
            "region": request.get("region"),
            "delay": request.get("delay"),
            "universe": request.get("universe"),
            "instrument_type": "EQUITY",
        },
        "simulation_budget": {
            "limit": task["budget"]["limit"],
            "used": task["budget"]["used"],
            "reserved": task["budget"]["reserved"],
        },
        "components": statuses,
        "next_required": next_required,
        "manual_manifest_required": False,
        "live_research_authorized": False,
        "brain_read_authorized": False,
        "model_call_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "permission_to_dispatch": False,
        "automatic_retry_allowed": False,
        "provider_invoked": False,
        "brain_invoked": False,
    }
