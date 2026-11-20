"""Pure domain workflow contracts shared by offline and future live adapters.

No gateway, model, filesystem or authorization side effects in this module.
Candidate qualification is owned by each domain workflow; this projection
never requests RA, PC/SC, platform data or performs a second evaluation.
"""
from types import SimpleNamespace

from alpha_research.orchestration.engine import OrchestrationError

WORKFLOW_COMMANDS = {
    "regular": "run-campaign",
    "super": "run-super",
    "optimization": "run-optimization",
}


def validate_workflow_request(workflow, request):
    if workflow not in WORKFLOW_COMMANDS:
        raise OrchestrationError("unknown workflow")
    if not isinstance(request, dict):
        raise OrchestrationError("workflow request must be an object")
    objective = request.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        raise OrchestrationError("objective is required")
    if workflow == "optimization":
        alpha_id = request.get("alpha_id")
        if not isinstance(alpha_id, str) or not alpha_id.strip():
            raise OrchestrationError("optimization requires Alpha ID")
    return {"workflow": workflow, "validated": True}


def command_and_args(workflow, request):
    validate_workflow_request(workflow, request)
    if type(request.get("max_batches", 1)) is not int or request.get("max_batches", 1) != 1:
        raise OrchestrationError(
            "unified orchestration currently supports a one-batch task only")
    return WORKFLOW_COMMANDS[workflow], SimpleNamespace(
        objective=request["objective"],
        alpha_id=request.get("alpha_id"),
        region=request.get("region"),
        delay=request.get("delay"),
        universe=request.get("universe"),
        category=request.get("category"),
        dataset=request.get("dataset_ids"),
        max_batches=1,
        ppa=bool(request.get("ppa_profile", request.get("ppa", False))),
    )


def project_existing_qualification(workflow, checkpoints):
    if workflow not in WORKFLOW_COMMANDS:
        raise OrchestrationError("unknown workflow")
    execution = checkpoints.get("execute")
    if not isinstance(execution, dict):
        raise OrchestrationError("missing execution checkpoint")
    if workflow == "regular":
        completed = execution.get("completed")
        if not isinstance(completed, list) or not completed:
            raise OrchestrationError("Regular Campaign lacks completed records")
        for item in completed:
            if (not isinstance(item, dict)
                    or not isinstance(item.get("alpha_ids"), list)
                    or not (isinstance(item.get("candidate_status"), dict)
                            or isinstance(item.get("candidate_status"), str)
                            and bool(item["candidate_status"].strip()))):
                raise OrchestrationError(
                    "Regular Campaign qualification evidence incomplete")
        return {"status": execution.get("status"), "completed": [
            {"alpha_ids": item["alpha_ids"],
             "candidate_status": item["candidate_status"]}
            for item in completed]}
    if not all(execution.get(key) for key in (
            "candidate_id", "candidate_status", "preferred_status",
            "candidate_ref", "preferred_ref")):
        raise OrchestrationError("qualification evidence unavailable")
    return {
        "candidate_id": execution["candidate_id"],
        "candidate_status": execution["candidate_status"],
        "preferred_status": execution["preferred_status"],
        "candidate_ref": execution["candidate_ref"],
        "preferred_ref": execution["preferred_ref"],
    }
