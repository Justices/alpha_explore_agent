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

    def __init__(self, state_root, config):
        self.root = state_root
        self.config = config

    def run(self, workflow, step, request, checkpoints, context=None):
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
