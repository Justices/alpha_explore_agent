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
from alpha_research.orchestration.execution_reservation import inspect_non_executable_reservation
from alpha_research.orchestration.simulation_attempt_admission import inspect_staged_simulation_attempt
from alpha_research.orchestration.operator_consent import inspect_consent_reservation


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
    from pathlib import Path
    root = Path(orchestrator.state_root)
    stop = root / "state" / "stop.flag"
    if stop.exists() or stop.is_symlink():
        blockers.append("GLOBAL_STOP_SIGNAL_PRESENT")
    if task.get("recovery_target") is not None:
        blockers.append("UNRESOLVED_RECOVERY_TARGET")
    if task.get("research_identity") is not None:
        blockers.append("DOMAIN_RESEARCH_ID_ALREADY_BOUND")
    from alpha_research.agents.governance import validate_policy
    try:
        validate_policy(governance_policy)
        facts["model_governance_valid"] = True
    except (ValueError, TypeError, KeyError):
        blockers.append("MODEL_GOVERNANCE_INVALID")
        facts["model_governance_valid"] = False
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
    # Inspect local SDK/CLI and credential *presence* only. Never return
    # credential values or claim remote provider/session availability.
    from alpha_research.agents.doctor import check_registered_roles
    try:
        readiness = check_registered_roles(model_config)
        facts["model_readiness"] = readiness
        if readiness["status"] != "READY_LOCAL":
            blockers.append("MODEL_ROLES_NOT_READY_LOCAL")
    except (OSError, ValueError, TypeError, KeyError, AttributeError,
            ImportError):
        blockers.append("MODEL_READINESS_INSPECTION_FAILED")
    # BRAIN authentication is an external session contract, deliberately
    # unverified by this read-only operation.
    facts["brain_session"] = "NOT_STARTED_NOT_VERIFIED"
    blockers.append("BRAIN_AUTHENTICATION_NOT_VERIFIED")
    blockers.append("LIVE_GATEWAY_OPERATION_GRANTS_NOT_BOUND")
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
        if review["status"] == "RECONCILIATION_REQUIRED":
            blockers.append("REVIEW_LEDGER_RECONCILIATION_REQUIRED")
    except (OrchestrationError, OSError, ValueError, TypeError, KeyError, AttributeError):
        blockers.append("REVIEW_LEDGER_UNREADABLE")
    try:
        reservation = inspect_non_executable_reservation(orchestrator, task_id)
        facts["execution_receipt_reservation"] = reservation["status"]
        if reservation["status"] == "RECONCILIATION_REQUIRED":
            blockers.append("EXECUTION_RECEIPT_LEDGER_RECONCILIATION_REQUIRED")
        elif reservation["status"] == "RESERVED_INSPECTION_ONLY":
            blockers.append("EXECUTION_RECEIPT_NOT_DISPATCH_AUTHORIZATION")
    except (OrchestrationError, OSError, ValueError, TypeError, KeyError, AttributeError):
        blockers.append("EXECUTION_RECEIPT_LEDGER_UNREADABLE")
    try:
        staged = inspect_staged_simulation_attempt(orchestrator, task_id)
        facts["simulation_attempt_staging"] = staged["status"]
        if staged["status"] == "RECONCILIATION_REQUIRED":
            blockers.append("SIMULATION_STAGING_RECONCILIATION_REQUIRED")
        elif staged["status"] == "STAGED_NO_DISPATCH":
            blockers.append("SIMULATION_STAGED_NOT_DISPATCH_AUTHORIZATION")
    except (OrchestrationError, OSError, ValueError, TypeError,
            KeyError, AttributeError):
        blockers.append("SIMULATION_STAGING_LEDGER_UNREADABLE")
    try:
        consent = inspect_consent_reservation(orchestrator, task_id)
        facts["operator_consent_audit"] = consent["status"]
        if consent["status"] == "RECONCILIATION_REQUIRED":
            blockers.append("OPERATOR_CONSENT_AUDIT_RECONCILIATION_REQUIRED")
        elif consent["status"] == "RESERVED_CONSENT_AUDIT_ONLY":
            blockers.append("OPERATOR_CONSENT_AUDIT_NOT_LIVE_AUTHORIZATION")
    except (OrchestrationError, OSError, ValueError, TypeError,
            KeyError, AttributeError):
        blockers.append("OPERATOR_CONSENT_AUDIT_UNREADABLE")
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
