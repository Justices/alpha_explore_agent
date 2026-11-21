"""P4.5 orchestrated Simulation write admission preflight (strictly read-only).

This is a reasoned *negative* decision, not a live dispatch grant.
It combines task, staged request, Intent, model budget and stop checks, and
requires a future independent trusted issuer/consumer. No BRAIN I/O occurs.
"""
from pathlib import Path

from alpha_research.agents.governance import ModelBudgetError, ModelCallLedger
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.mock_workspace import marker_path
from alpha_research.orchestration.operator_consent import inspect_consent_reservation
from alpha_research.orchestration.simulation_attempt_admission import (
    inspect_staged_simulation_attempt,
)


def inspect_simulation_dispatch_gate(orchestrator, task_id, policy):
    task = orchestrator.load(task_id)
    blockers = []
    facts = {"workflow": task["workflow"], "task_status": task["status"],
             "execution_budget": dict(task["budget"])}
    if task["workflow"] not in {"super", "optimization"}:
        blockers.append("SIMULATION_WORKFLOW_NOT_SUPPORTED")
    if task["status"] != "CREATED":
        blockers.append("TASK_NOT_FRESH")
    if task["budget"] != {"limit": 1, "used": 0, "reserved": 0}:
        blockers.append("TASK_BUDGET_NOT_FRESH")
    root = Path(orchestrator.state_root)
    stop = root / "state" / "stop.flag"
    if stop.exists() or stop.is_symlink():
        blockers.append("GLOBAL_STOP_SIGNAL_PRESENT")
    marker = marker_path(root)
    if marker.exists() or marker.is_symlink():
        blockers.append("MOCK_ROOT_NOT_ALLOWED")
    staged = inspect_staged_simulation_attempt(orchestrator, task_id)
    facts["staging_status"] = staged["status"]
    if staged["status"] != "STAGED_NO_DISPATCH":
        blockers.append("SIMULATION_INTENT_NOT_STAGED_OR_UNSAFE")
    # External signatures are evidence, not production consent authority.
    # A one-shot audit reservation never becomes a Gateway dispatch token.
    consent = inspect_consent_reservation(orchestrator, task_id)
    facts["operator_consent_audit_status"] = consent["status"]
    if consent["status"] == "RECONCILIATION_REQUIRED":
        blockers.append("OPERATOR_CONSENT_AUDIT_RECONCILIATION_REQUIRED")
    elif consent["status"] == "RESERVED_CONSENT_AUDIT_ONLY":
        blockers.append("OPERATOR_CONSENT_AUDIT_NOT_LIVE_AUTHORIZATION")
    else:
        blockers.append("OPERATOR_CONSENT_NOT_RESERVED")
    # Task bindings are structured objects; they are not plain strings.
    # Inspecting the real persisted shape is required to see unresolved
    # model attempts, particularly while auditing a blocked RUNNING task.
    binding = task.get("research_identity")
    research_id = (binding.get("research_id")
                   if isinstance(binding, dict)
                   and binding.get("workflow") == task["workflow"] else None)
    if not isinstance(research_id, str) or not research_id.strip():
        blockers.append("RESEARCH_ID_NOT_BOUND")
    else:
        if (staged["status"] == "STAGED_NO_DISPATCH"
                and staged.get("research_id") != research_id):
            blockers.append("SIMULATION_RESEARCH_ID_MISMATCH")
        try:
            # The ordinary status() takes a writer lock and updates its
            # metadata file. Audit must remain genuinely read-only.
            ledger = ModelCallLedger(root, policy)
            record_path, _ = ledger._paths(research_id)
            state = ledger._load(record_path, research_id)
            budget = ledger._summarize(state)
            # Unreported tokens are not a cost bound. A successful call
            # with unknown usage must not be silently counted as free.
            budget["blocked"] = bool(
                budget["unknown"] or budget["unresolved"]
                or budget["unknown_usage_attempts"]
                or budget["attempts"] >= ledger.policy["max_calls"]
                or budget["reported_tokens"]
                   >= ledger.policy["max_reported_tokens"])
            facts["model_budget"] = {
                "attempts": budget["attempts"],
                "unknown": budget["unknown"],
                "unresolved": budget["unresolved"],
                "unknown_usage_attempts": budget["unknown_usage_attempts"],
                "blocked": budget["blocked"],
            }
            if budget["unknown"] or budget["unresolved"]:
                blockers.append("MODEL_ATTEMPT_RECONCILIATION_REQUIRED")
            if budget["unknown_usage_attempts"]:
                blockers.append("MODEL_USAGE_UNVERIFIED")
            if budget["blocked"]:
                blockers.append("MODEL_BUDGET_BLOCKED")
        except (ModelBudgetError, OSError, TypeError, ValueError, KeyError):
            blockers.append("MODEL_BUDGET_UNREADABLE")
    # Admission cannot be inferred from receipt signature, staging, mock
    # success, or presence of BRAIN credentials. Explicit trusted consent
    # issuance/atomic consumption and driver grant validation are unavailable.
    blockers.extend((
        "TRUSTED_OPERATOR_CONSENT_NOT_VERIFIED",
        "TRUSTED_LIVE_GRANT_CONSUMER_NOT_IMPLEMENTED",
        "DRIVER_OPERATION_GRANT_NOT_IMPLEMENTED",
        "PRODUCTION_ADAPTER_DISABLED",
    ))
    return {
        "schema_version": 1, "task_id": task_id,
        "status": "NOT_AUTHORIZED", "audit_mode": "READ_ONLY",
        "facts": facts, "blockers": sorted(set(blockers)),
        "automatic_retry_allowed": False,
        "permission_to_dispatch": False,
        "model_call_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "brain_invoked": False,
        "provider_invoked": False,
    }
