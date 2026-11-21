"""P4.7.4 read-only Simulation recovery classification, never replay.

This module makes no platform session or provider call, never acquires a
writer lock, never transitions Intent, and never grants dispatch. A local
POSTING or OUTCOME_UNKNOWN record cannot prove whether a remote POST happened.
Only an independently authorized platform readback can resolve that ambiguity.
"""
import json
from pathlib import Path

from alpha_research.agents.governance import ModelBudgetError, ModelCallLedger
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.operator_consent import inspect_consent_reservation
from alpha_research.orchestration.pre_dispatch_escrow import inspect_pre_dispatch_escrow
from alpha_research.orchestration.simulation_attempt_admission import (
    _paths as _staging_paths, inspect_staged_simulation_attempt,
)
from alpha_research.state.fingerprint import fingerprint_json


_ISSUE = "RECONCILIATION_REQUIRED"
_KNOWN = frozenset({
    "PREPARED", "POSTING", "OUTCOME_UNKNOWN", "RESPONSE_RECEIVED", "READ_BACK",
})


def inspect_simulation_recovery_evidence(orchestrator, task_id, policy):
    """Classify saved evidence without credentials, network or filesystem writes."""
    task = orchestrator.load(task_id)
    staged = inspect_staged_simulation_attempt(orchestrator, task_id)
    consent = inspect_consent_reservation(orchestrator, task_id)
    escrow = inspect_pre_dispatch_escrow(orchestrator, task_id, policy)
    _, intent_path, _ = _staging_paths(orchestrator, task_id)
    reasons = []
    for state, label in ((staged, "SIMULATION_STAGING"),
                         (consent, "CONSENT_AUDIT"),
                         (escrow, "PRE_DISPATCH_ESCROW")):
        if state["status"] == _ISSUE:
            reasons.append(label + "_RECONCILIATION_REQUIRED")
    facts = {
        "task_status": task["status"],
        "staging_status": staged["status"],
        "consent_status": consent["status"],
        "escrow_status": escrow["status"],
    }
    model_status = "DOMAIN_IDENTITY_NOT_PROVABLE"
    research_id = None
    if isinstance(task.get("research_identity"), dict):
        research_id = task["research_identity"].get("research_id")
    if not isinstance(research_id, str) or not research_id:
        research_id = staged.get("research_id")
    if isinstance(research_id, str) and research_id:
        try:
            ledger = ModelCallLedger(orchestrator.state_root, policy)
            path, _ = ledger._paths(research_id)
            if not path.is_file() or path.is_symlink() or path.parent.is_symlink():
                reasons.append("MODEL_BUDGET_HISTORY_NOT_PROVABLE")
            state = ledger._load(path, research_id)
            summary = ledger._summarize(state)
            facts["model_budget"] = {
                "attempts": summary["attempts"],
                "unresolved": summary["unresolved"],
                "unknown": summary["unknown"],
                "unknown_usage_attempts": summary["unknown_usage_attempts"],
            }
            if summary["unknown"] or summary["unresolved"]:
                reasons.append("MODEL_ATTEMPT_RECONCILIATION_REQUIRED")
            if summary["unknown_usage_attempts"]:
                reasons.append("MODEL_TOKEN_USAGE_UNVERIFIED")
            model_status = "MODEL_BUDGET_INSPECTED"
        except (ModelBudgetError, OSError, ValueError, TypeError, KeyError):
            reasons.append("MODEL_BUDGET_UNREADABLE")
    else:
        reasons.append("RESEARCH_ID_NOT_BOUND")
    facts["model_status"] = model_status

    state = "INTENT_MISSING_OR_UNSAFE"
    if intent_path.is_symlink() or not intent_path.is_file():
        reasons.append("INTENT_MISSING_OR_SYMLINKED")
    else:
        try:
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            if (not isinstance(intent, dict)
                    or intent.get("workflow") != task["workflow"]
                    or not isinstance(intent.get("request"), dict)
                    or not isinstance(intent.get("settings"), dict)
                    or not isinstance(intent.get("metadata"), dict)
                    or intent["metadata"].get("task_id") != task_id
                    or not isinstance(intent.get("run_id"), str)
                    or not isinstance(intent.get("batch_id"), str)
                    or intent.get("request_sha256") != fingerprint_json({
                        "request": intent["request"],
                        "settings": intent["settings"],
                    })
                    or intent.get("status") not in _KNOWN):
                raise ValueError("Simulation Intent identity, digest or status invalid")
            state = intent["status"]
            facts["intent_sha256"] = fingerprint_json(intent)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            reasons.append("INTENT_UNREADABLE_OR_DRIFTED")
    facts["intent_status"] = state

    if state in {"POSTING", "OUTCOME_UNKNOWN"}:
        # No inference can establish that a remote POST did not happen.
        classification = "PLATFORM_READBACK_REQUIRED"
        reasons.append("REMOTE_OUTCOME_UNVERIFIED")
    elif state == "RESPONSE_RECEIVED":
        classification = "LOCAL_RESPONSE_RECONCILIATION_REQUIRED"
        reasons.append("REMOTE_RESPONSE_NOT_INDEPENDENTLY_VERIFIED")
    elif state == "READ_BACK":
        classification = "READBACK_PROVENANCE_REVIEW_REQUIRED"
        reasons.append("RECEIPT_PLATFORM_ORIGIN_NOT_VERIFIED")
    elif state == "PREPARED" and escrow["status"] == "PREDISPATCH_ESCROW_NOT_EXECUTABLE":
        classification = "OFFLINE_ESCROW_ONLY_NO_POST_EVIDENCE"
        reasons.append("NO_LIVE_DISPATCH_AUTHORIZATION")
    else:
        classification = _ISSUE
        reasons.append("SIMULATION_INTENT_NOT_READY")
    return {
        "schema_version": 1,
        "task_id": task_id, "status": classification,
        "audit_mode": "READ_ONLY",
        "facts": facts, "blockers": sorted(set(reasons)),
        "recommendation": (
            "Independent, explicitly authorized platform READBACK and operator "
            "review required; no POST or automatic retry"
            if state in {"POSTING", "OUTCOME_UNKNOWN", "RESPONSE_RECEIVED", "READ_BACK"}
            else "Review local evidence and missing external consent; no POST"
        ),
        "permission_to_dispatch": False,
        "automatic_retry_allowed": False,
        "remote_outcome_proven": False,
        "brain_invoked": False,
        "provider_invoked": False,
    }
