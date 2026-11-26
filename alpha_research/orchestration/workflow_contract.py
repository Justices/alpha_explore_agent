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
    if workflow == "regular":
        max_batches = request.get("max_batches", 1)
        if type(max_batches) is not int or not 1 <= max_batches <= 20:
            raise OrchestrationError("Regular max_batches must be an integer from 1 to 20")
    else:
        max_iterations = request.get("max_iterations", 1)
        if type(max_iterations) is not int or not 1 <= max_iterations <= 20:
            raise OrchestrationError("max_iterations must be an integer from 1 to 20")
        max_batches = request.get("max_batches", 1)
        if type(max_batches) is not int or max_batches != 1:
            raise OrchestrationError("max_batches applies only to Regular Campaigns")
    return {"workflow": workflow, "validated": True}


def command_and_args(workflow, request):
    validate_workflow_request(workflow, request)
    max_batches = request.get("max_batches", 1)
    max_iterations = request.get("max_iterations", 1)
    return WORKFLOW_COMMANDS[workflow], SimpleNamespace(
        objective=request["objective"],
        alpha_id=request.get("alpha_id"),
        region=request.get("region"),
        delay=request.get("delay"),
        universe=request.get("universe"),
        category=request.get("category"),
        dataset=request.get("dataset_ids"),
        max_batches=max_batches,
        max_iterations=max_iterations,
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
