"""Read-only P2 recovery preflight: no model calls, BRAIN calls or POST replay.

The operator still must use RecoveryCoordinator with a separately authorized
gateway to reconcile any uncertain platform operation. Preflight never
releases execution budget or modifies a Simulation Intent.
"""
from pathlib import Path

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.execution_identity import inspect_domain_research_handoff
from alpha_research.orchestration.recovery import RecoveryCoordinator, RecoveryError


def inspect_task_recovery_preflight(orchestrator, task_id, model_config, policy):
    task = orchestrator.load(task_id)
    handoff = inspect_domain_research_handoff(
        orchestrator, task_id, model_config, policy)
    failure = task.get("failure") or {}
    if (task.get("status") != "BLOCKED"
            or failure.get("step") != "execute"
            or task.get("steps", {}).get("execute") != "RUNNING"):
        return {"task_id": task_id, "status": "NOT_BLOCKED_EXECUTE",
                "automatic_replay_allowed": False, "brain_invoked": False}
    target = task.get("recovery_target")
    if not isinstance(target, dict) or not target.get("ref"):
        return {"task_id": task_id, "status": "MISSING_DURABLE_INTENT",
                "automatic_replay_allowed": False, "brain_invoked": False}
    kind = "regular_batch" if task["workflow"] == "regular" else "simulation_intent"
    if target.get("kind") != kind:
        raise OrchestrationError("recovery target kind does not match workflow")
    coordinator = RecoveryCoordinator(orchestrator.state_root)
    try:
        path = coordinator._resolve_target(target["ref"])
        workflow, _, intent, _ = coordinator._inspect(path)
    except RecoveryError:
        return {"task_id": task_id, "status": "INTENT_REQUIRES_MANUAL_INSPECTION",
                "automatic_replay_allowed": False, "brain_invoked": False}
    if workflow != task["workflow"]:
        raise OrchestrationError("recovery intent workflow does not match task")
    budget = handoff.get("model_budget")
    if not isinstance(budget, dict):
        return {"task_id": task_id, "status": "DOMAIN_IDENTITY_NOT_BOUND",
                "automatic_replay_allowed": False, "brain_invoked": False}
    if budget.get("unknown", 0) or budget.get("unresolved", 0):
        status = "MODEL_ATTEMPT_RECONCILIATION_REQUIRED"
    else:
        status = "PLATFORM_RECONCILIATION_REQUIRED"
    return {
        "task_id": task_id,
        "workflow": task["workflow"],
        "research_id": handoff["research_id"],
        "intent_status": intent["status"],
        "status": status,
        "budget_reserved": task["budget"]["reserved"],
        "model_unknown": budget["unknown"],
        "model_unresolved": budget["unresolved"],
        "automatic_replay_allowed": False,
        "brain_invoked": False,
    }
