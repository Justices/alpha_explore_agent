"""Read-only P3 admission audit. Never issues authorization or a dispatch token.

This is deliberately a negative readiness contract: a prepared proposal,
signed review, and valid model policy are independently inspectable, but none
authorizes a provider invocation, BRAIN simulation or Alpha submission.
"""
from alpha_research.orchestration.approval_review import inspect_review_reservation
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.execution_identity import inspect_execution_identity
from alpha_research.orchestration.execution_proposal import inspect_execution_proposal
from alpha_research.orchestration.mock_workspace import marker_path, inspect_mock_workspace


def audit_execution_admission(orchestrator, task_id, model_config,
                              governance_policy, *, config_source):
    """Produce a stable fail-closed audit without filesystem writes or APIs."""
    blockers = []
    facts = {}
    task = orchestrator.load(task_id)
    facts["task_status"] = task["status"]
    facts["workflow"] = task["workflow"]
    facts["budget"] = task["budget"].copy()
    if task["status"] != "CREATED":
        blockers.append("TASK_NOT_UNSTARTED")
    if task["budget"] != {"limit": 1, "used": 0, "reserved": 0}:
        blockers.append("EXECUTION_BUDGET_NOT_FRESH")
    try:
        proposal = inspect_execution_proposal(orchestrator, task_id)
        facts["proposal_sha256"] = proposal["proposal_sha256"]
    except (OrchestrationError, OSError, ValueError, TypeError, KeyError, AttributeError):
        blockers.append("PROPOSAL_MISSING_OR_DRIFTED")
    try:
        identity = inspect_execution_identity(
            orchestrator, task_id, model_config, governance_policy)
        facts["identity_sha256"] = identity["identity_sha256"]
        pinned = identity["identity"]
        if (pinned.get("allow_model_call") is not False
                or pinned.get("allow_brain_simulation") is not False
                or pinned.get("allow_alpha_submission") is not False):
            blockers.append("IDENTITY_NOT_FAIL_CLOSED")
    except (OrchestrationError, OSError, ValueError, TypeError, KeyError, AttributeError):
        blockers.append("MODEL_IDENTITY_MISSING_OR_DRIFTED")
    facts["config_source"] = config_source
    if config_source != "user_config":
        blockers.append("OFFLINE_DEFAULTS_NOT_LIVE_CONFIG")
    try:
        if (marker_path(orchestrator.state_root).exists()
                or marker_path(orchestrator.state_root).is_symlink()):
            inspect_mock_workspace(orchestrator.state_root)
            blockers.append("OFFLINE_MOCK_WORKSPACE")
    except (OrchestrationError, OSError, ValueError, TypeError, KeyError, AttributeError):
        blockers.append("INVALID_MOCK_WORKSPACE")
    try:
        review = inspect_review_reservation(orchestrator, task_id)
        facts["review_status"] = review["status"]
    except (OrchestrationError, OSError, ValueError, TypeError, KeyError, AttributeError):
        blockers.append("REVIEW_LEDGER_UNREADABLE")
    # Review receipts cover only proposal review, not model or BRAIN writes.
    blockers.append("TRUSTED_EXECUTION_AUTHORIZATION_NOT_IMPLEMENTED")
    blockers.append("PRODUCTION_DISPATCH_ADAPTER_DISABLED")
    return {
        "schema_version": 1,
        "task_id": task_id,
        "status": "NOT_AUTHORIZED",
        "audit_mode": "READ_ONLY",
        "facts": facts,
        "blockers": sorted(set(blockers)),
        "model_call_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "provider_invoked": False,
        "brain_invoked": False,
    }
