"""Adapter wiring existing V1.1 entrypoints to the offline orchestrator.

The adapter is intentionally mock-only. It accepts a caller-owned fake gateway
and fake agent, but rejects the production Gateway type. The underlying V1.1
executors already perform qualification; this adapter never qualifies twice.
"""
from types import SimpleNamespace

from .entrypoints import dispatch
from .orchestrator import OrchestrationError, pool_snapshot


class MockWorkflowAdapter:
    def __init__(self, state_root, config, gateway, agent, *,
                 dispatcher=dispatch):
        if not getattr(gateway, "is_offline_mock", False):
            raise OrchestrationError("explicit offline mock gateway is required")
        if agent is None:
            raise OrchestrationError("mock agent is required")
        if dispatcher is dispatch and not getattr(getattr(agent, "model", None),
                                                  "is_offline_mock", False):
            raise OrchestrationError("explicit offline mock model is required")
        self.root = state_root
        self.config = config
        self.gateway = gateway
        self.agent = agent
        self.dispatcher = dispatcher

    def run(self, workflow, step, request, checkpoints, context=None):
        if workflow not in ("regular", "super", "optimization"):
            raise OrchestrationError("unknown workflow")
        if step == "validate":
            if not request.get("objective"):
                raise OrchestrationError("objective is required")
            if workflow == "optimization" and not request.get("alpha_id"):
                raise OrchestrationError("optimization requires Alpha ID")
            return {"workflow": workflow, "validated": True, "mode": "mock"}
        if step == "execute":
            command = {"regular": "run-campaign", "super": "run-super",
                       "optimization": "run-optimization"}[workflow]
            args = SimpleNamespace(
                objective=request["objective"],
                alpha_id=request.get("alpha_id"),
                region=request.get("region"),
                delay=request.get("delay"),
                universe=request.get("universe"),
                category=request.get("category"),
                dataset=request.get("dataset_ids"),
                max_batches=request.get("max_batches", 1),
                ppa=bool(request.get("ppa", False)),
            )
            if context is None:
                result = self.dispatcher(command, args, self.config, self.root,
                                         self.gateway, self.agent)
            else:
                result = self.dispatcher(command, args, self.config, self.root,
                                         self.gateway, self.agent,
                                         execution_context=context)
            if not isinstance(result, dict):
                raise OrchestrationError("research executor returned non-object")
            return result
        if step == "qualify":
            execution = checkpoints.get("execute")
            if not isinstance(execution, dict):
                raise OrchestrationError("missing execution checkpoint")
            # Each existing workflow performs CandidateQualificationService
            # internally. Do not repeat RA, PC/SC or platform calls.
            if workflow == "regular":
                completed = execution.get("completed")
                if not isinstance(completed, list):
                    raise OrchestrationError("Regular Campaign lacks completed records")
                if not completed:
                    raise OrchestrationError("Regular Campaign has no completed batch")
                for item in completed:
                    if (not isinstance(item, dict) or
                            not isinstance(item.get("alpha_ids"), list) or
                            not (isinstance(item.get("candidate_status"), dict)
                                 or isinstance(item.get("candidate_status"), str)
                                 and bool(item["candidate_status"].strip()))):
                        raise OrchestrationError("Regular Campaign qualification evidence incomplete")
                return {"status": execution.get("status"), "completed": [
                    {"alpha_ids": item["alpha_ids"],
                     "candidate_status": item["candidate_status"]}
                    for item in completed]}
            if not all(execution.get(key) for key in (
                    "candidate_id", "candidate_status", "preferred_status",
                    "candidate_ref", "preferred_ref")):
                raise OrchestrationError("qualification evidence unavailable")
            return {"candidate_id": execution["candidate_id"],
                    "candidate_status": execution.get("candidate_status"),
                    "preferred_status": execution.get("preferred_status"),
                    "candidate_ref": execution.get("candidate_ref"),
                    "preferred_ref": execution.get("preferred_ref")}
        if step == "collect":
            return {"qualification": checkpoints.get("qualify"),
                    "pool": pool_snapshot(self.root, self.config)}
        raise OrchestrationError("unknown orchestration step")
