"""Adapter wiring existing V1.1 entrypoints to the offline orchestrator.

The adapter is intentionally mock-only. It accepts a caller-owned fake gateway
and fake agent, but rejects the production Gateway type. The underlying V1.1
executors already perform qualification; this adapter never qualifies twice.
"""
import copy

from alpha_research.orchestration.dispatch import dispatch
from alpha_research.orchestration.engine import OrchestrationError, pool_snapshot
from alpha_research.orchestration.workflow_contract import (
    validate_workflow_request, command_and_args, project_existing_qualification,
)


class OfflineResearchAdapter:
    """Explicit fixture adapter; cannot call the platform or submit Alphas."""
    def __init__(self, fixtures):
        if not isinstance(fixtures, dict):
            raise OrchestrationError("fixtures must be an object")
        self.fixtures = fixtures

    def run(self, workflow, step, request, checkpoints, context=None):
        key = workflow + "." + step
        if key not in self.fixtures:
            raise OrchestrationError("missing offline fixture: " + key)
        result = self.fixtures[key]
        if not isinstance(result, dict):
            raise OrchestrationError("offline fixture must be an object")
        if step == "collect":
            result = copy.deepcopy(result)
            result["qualification"] = checkpoints.get("qualify")
        return copy.deepcopy(result)


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
        if step == "validate":
            return {**validate_workflow_request(workflow, request), "mode": "mock"}
        if step == "execute":
            command, args = command_and_args(workflow, request)
            emit = (context or {}).get("emit_event")
            if callable(emit):
                emit("domain.dispatch_started", {"command": command})
            if context is None or self.dispatcher is not dispatch:
                result = self.dispatcher(command, args, self.config, self.root,
                                         self.gateway, self.agent)
            else:
                result = self.dispatcher(command, args, self.config, self.root,
                                         self.gateway, self.agent,
                                         execution_context=context)
            if not isinstance(result, dict):
                raise OrchestrationError("research executor returned non-object")
            emit = (context or {}).get("emit_event")
            if callable(emit):
                emit("domain.dispatch_completed", {
                    "command": command,
                    "result_keys": sorted(result),
                    **{key: result[key] for key in (
                        "status", "campaign_id", "run_id", "branch_id",
                        "alpha_id", "candidate_alpha_id", "candidate_id",
                        "candidate_status", "preferred_status", "record_ref",
                        "state_path", "report_ref", "batches_completed")
                       if isinstance(result.get(key), (str, int, float, bool))},
                })
            return result
        if step == "qualify":
            return project_existing_qualification(workflow, checkpoints)
        if step == "collect":
            return {"qualification": checkpoints.get("qualify"),
                    "pool": pool_snapshot(self.root, self.config)}
        raise OrchestrationError("unknown orchestration step")
