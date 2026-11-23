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
from alpha_research.orchestration.research_readiness_matrix import (
    inspect_p6_integrated_release,
)
from alpha_research.orchestration.release_matrix import (
    inspect_research_release,
)

_COMPONENTS = ("research_preparation", "model_attempt", "manifest", "grant_audit",
               "catalog_contract", "live_plan")
# An unexpected component verdict is NOT a new success state. Keep this
# contract explicit so future component changes must update the aggregator.
_KNOWN_STATUSES = {
    "research_preparation": {
        "AWAITING_RESEARCH_PREPARATION", "PREPARING",
        "BLOCKED_MANUAL_REVIEW", "RESEARCH_PREPARED_OFFLINE",
    },
    "model_attempt": {
        "NO_MODEL_ATTEMPT", "MODEL_CALL_MAY_HAVE_STARTED",
        "MODEL_MOCK_UNKNOWN", "MODEL_MOCK_COMPLETED",
        "RECONCILIATION_REQUIRED",
    },
    "manifest": {
        "AWAITING_RESEARCH_OUTPUT", "GENERATED_LOCAL_NOT_AUTHORIZED",
    },
    "grant_audit": {
        "NOT_RESERVED", "RESERVED_RESEARCH_GRANT_AUDIT_ONLY",
        "RECONCILIATION_REQUIRED",
    },
    "catalog_contract": {"CATALOG_PROTOCOL_VALIDATED_NO_LIVE_DRIVER"},
}


def _status(component, task_id, callback):
    try:
        record = callback()
        if (not isinstance(record, dict)
                or record.get("status") not in _KNOWN_STATUSES[component]
                or record.get("task_id") != task_id
                or type(record.get("schema_version")) is not int
                or record["schema_version"] != 1
                or record.get("permission_to_dispatch") is not False):
            raise OrchestrationError("research status component contract drift")
        return record["status"]
    except (OrchestrationError, OSError, ValueError, TypeError, KeyError):
        # Reporting must never "fix" evidence or silently reset a failed
        # journal. Keep the source untouched for manual investigation.
        return "RECONCILIATION_REQUIRED"


def _negative_release_projection(orchestrator, task_id, inspector,
                                 expected_status, fallback_blocker):
    """Project a blocked release report without trusting an arbitrary dict.

    Reconciliation failures never become a success status, and this read-only
    aggregator must not fix, replace or retry untrusted source evidence.
    """
    try:
        report = inspector(orchestrator, task_id)
        blockers = report.get("blockers") if isinstance(report, dict) else None
        if (not isinstance(report, dict)
                or type(report.get("schema_version")) is not int
                or report["schema_version"] != 1
                or report.get("task_id") != task_id
                or report.get("status") != expected_status
                or report.get("audit_mode") != "READ_ONLY"
                or not isinstance(blockers, list) or not blockers
                or any(not isinstance(b, str) or not b for b in blockers)
                or any(report.get(flag) is not False for flag in (
                    "permission_to_dispatch", "production_authorized",
                    "model_call_authorized", "brain_read_authorized",
                    "brain_simulation_authorized", "alpha_submission_authorized",
                    "automatic_retry_allowed"))):
            raise OrchestrationError("research release projection contract drift")
        return report["status"], sorted(set(blockers))
    except (OrchestrationError, OSError, ValueError, TypeError, KeyError):
        return "RECONCILIATION_REQUIRED", [fallback_blocker]


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
            "research_preparation", task_id,
            lambda: inspect_research_preparation(orchestrator, task_id)),
        "model_attempt": _status(
            "model_attempt", task_id,
            lambda: inspect_runtime_attempt(orchestrator, task_id)),
        "manifest": _status(
            "manifest", task_id,
            lambda: inspect_generated_manifest(orchestrator, task_id)),
        "grant_audit": _status(
            "grant_audit", task_id,
            lambda: inspect_research_grant_claim(orchestrator, task_id)),
        "catalog_contract": _status(
            "catalog_contract", task_id,
            lambda: inspect_catalog_read_contract(orchestrator, task_id)),
    }
    try:
        live_plan = inspect_research_runtime_plan(
            orchestrator, task_id, model_config, governance_policy)
        if (not isinstance(live_plan, dict)
                or live_plan.get("status") != "RESEARCH_RUNTIME_LIVE_BLOCKED"
                or live_plan.get("task_id") != task_id
                or type(live_plan.get("schema_version")) is not int
                or live_plan["schema_version"] != 1
                or live_plan.get("permission_to_dispatch") is not False
                or live_plan.get("live_research_authorized") is not False
                or not isinstance(live_plan.get("blockers"), list)
                or not live_plan["blockers"]
                or any(not isinstance(v, str) or not v
                       for v in live_plan["blockers"])):
            raise OrchestrationError("invalid research runtime plan")
    except (OrchestrationError, OSError, ValueError, TypeError, KeyError):
        live_plan = {
            "status": "RECONCILIATION_REQUIRED",
            "blockers": ["RESEARCH_RUNTIME_PLAN_UNAVAILABLE"],
        }
    statuses["live_plan"] = live_plan["status"]
    # The unified read-only status projects each release matrix through
    # one strict negative-only contract. Neither result is a live grant.
    production_status, production_blockers = _negative_release_projection(
        orchestrator, task_id, inspect_p6_integrated_release,
        "P6_PRODUCTION_BLOCKED", "P6_READINESS_EVIDENCE_UNAVAILABLE")
    p7_status, p7_blockers = _negative_release_projection(
        orchestrator, task_id, inspect_research_release,
        "P7_PRODUCTION_BLOCKED", "P7_READINESS_EVIDENCE_UNAVAILABLE")
    if set(statuses) != set(_COMPONENTS):
        raise OrchestrationError("research status component list drift")
    uncertain = {
        "RECONCILIATION_REQUIRED", "MODEL_CALL_MAY_HAVE_STARTED",
        "MODEL_MOCK_UNKNOWN", "BLOCKED_MANUAL_REVIEW", "PREPARING",
    }
    needs_reconciliation = {
        key for key, status in statuses.items() if status in uncertain
    }
    # Completion is a *chain*: a Manifest alone is not proof that the
    # offline Agent ran and its model attempt was durably reconciled.
    prepared = statuses["research_preparation"] == "RESEARCH_PREPARED_OFFLINE"
    model_complete = statuses["model_attempt"] == "MODEL_MOCK_COMPLETED"
    manifest_ready = statuses["manifest"] == "GENERATED_LOCAL_NOT_AUTHORIZED"
    if prepared and (not model_complete or not manifest_ready):
        needs_reconciliation.add("research_preparation")
    if model_complete and not prepared:
        needs_reconciliation.add("model_attempt")
    if production_status == "RECONCILIATION_REQUIRED":
        needs_reconciliation.add("production_readiness")
    if p7_status == "RECONCILIATION_REQUIRED":
        needs_reconciliation.add("p7_production_readiness")
    needs_reconciliation = sorted(set(needs_reconciliation))
    if needs_reconciliation:
        state = "RECONCILIATION_REQUIRED"
        next_required = "MANUAL_STATE_AND_EVIDENCE_RECONCILIATION"
    elif prepared and model_complete and manifest_ready:
        state = "AWAITING_INDEPENDENT_RESEARCH_AUTHORIZATION"
        next_required = "INDEPENDENT_RESEARCH_ISSUER_AND_GLOBAL_CONSUMER"
    else:
        state = "RESEARCH_OUTPUT_NOT_READY_LIVE_BLOCKED"
        next_required = (
            "VERIFY_RESEARCH_PREPARATION_CHAIN" if manifest_ready
            else "AUTHORIZED_RESEARCH_AGENT_CANDIDATE_GENERATION"
        )
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
        "live_blockers": sorted(set(live_plan["blockers"])),
        "p6_production_status": production_status,
        "p6_production_blockers": sorted(set(production_blockers)),
        "p7_production_status": p7_status,
        "p7_production_blockers": sorted(set(p7_blockers)),
        "reconciliation_components": needs_reconciliation,
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
