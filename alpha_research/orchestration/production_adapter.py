"""Production orchestration boundary: deliberately disabled for external writes.

This module establishes the *separate* production adapter interface without
granting a new BRAIN execution path. A later integration must reuse the CLI's
authentication, role selection, model budget, process lock and domain recovery
coordinator, then add an explicit, durable Simulation approval contract.
Until then execute ALWAYS fails closed, even when a caller passes an agent.
"""
from alpha_research.orchestration.engine import OrchestrationError, pool_snapshot
from alpha_research.orchestration.workflow_contract import (
    validate_workflow_request, project_existing_qualification,
)


class ProductionWorkflowAdapter:
    """Read-only control-plane steps; never invokes a model or BRAIN.

    Do not add a boolean `live=True` shortcut here: a real write requires a
    separate audited approval receipt, an owned gateway and a pinned task.
    """

    def __init__(self, state_root, config, *, offline_test_adapter=None):
        self.root = state_root
        self.config = config
        self.offline_test_adapter = None
        if offline_test_adapter is not None:
            # The ONLY enabled execution path is a real MockWorkflowAdapter
            # backed by the project's offline gateway AND offline model.
            # A custom dispatcher is rejected: it could call a live service.
            from pathlib import Path
            from alpha_research.orchestration.offline_adapter import MockWorkflowAdapter
            from alpha_research.orchestration.dispatch import dispatch
            if (type(offline_test_adapter) is not MockWorkflowAdapter
                    or offline_test_adapter.dispatcher is not dispatch
                    or not getattr(offline_test_adapter.gateway, "is_offline_mock", False)
                    or not getattr(
                        getattr(offline_test_adapter.agent, "model", None),
                        "is_offline_mock", False)
                    or Path(offline_test_adapter.root).resolve() != Path(state_root).resolve()):
                raise OrchestrationError(
                    "production integration requires the project offline mock runtime")
            self.offline_test_adapter = offline_test_adapter

    def run(self, workflow, step, request, checkpoints, context=None):
        if self.offline_test_adapter is not None:
            return self.offline_test_adapter.run(
                workflow, step, request, checkpoints, context=context)
        if step == "validate":
            validate_workflow_request(workflow, request)
            # Refuse before execute enters RUNNING or reserves its budget.
            raise OrchestrationError(
                "production orchestration is not authorized; "
                "no external operation was attempted")
        if step == "execute":
            raise OrchestrationError(
                "production orchestration execution requires separately "
                "reviewed authorization and recovery integration")
        if step == "qualify":
            return project_existing_qualification(workflow, checkpoints)
        if step == "collect":
            return {"qualification": checkpoints.get("qualify"),
                    "pool": pool_snapshot(self.root, self.config)}
        raise OrchestrationError("unknown orchestration step")
