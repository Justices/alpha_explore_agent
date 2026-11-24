"""Task-bound bridge from the active CLI runtime to the REAL V1.1 workflows.

No new BRAIN or model client is created. The bridge is intentionally NOT
registered as an unrestricted CLI or P7 release entrypoint: an independently
trusted, one-use Simulation authority consumer is required for each Task.
Fail closed when absent. A caller-supplied bool, signed-file path or local
audit report is never accepted in place of an external consumer.

This provides the missing production *wiring* without pretending that the
independent production authority already exists.
"""
from pathlib import Path

from alpha_research.integrations.brain.gateway import Gateway
from alpha_research.orchestration.dispatch import dispatch
from alpha_research.orchestration.engine import OrchestrationError, pool_snapshot
from alpha_research.orchestration.mock_workspace import marker_path
from alpha_research.orchestration.runtime_context import CliRuntimeContext
from alpha_research.orchestration.workflow_contract import (
    command_and_args, project_existing_qualification, validate_workflow_request,
)
from alpha_research.state.fingerprint import fingerprint_json
from alpha_research.state.store import StopSignals

_SIMULATION = frozenset(("simulate_batch", "simulate_super", "simulate_canary_once"))
_MUTATIONS = frozenset(("tag_alpha", "sync_backtest_cache", "submit_alpha",
                        "set_alpha_properties"))
_READ_SETUP = frozenset(("set_case", "validate_simulation_settings"))


class BoundedSimulationGateway:
    """One owner, exact write budget; stop before the next non-idempotent call.

    The domain's normal Gateway keeps its existing command and RA-gate policy.
    This wrapper does not perform a POST retry; a transport exception consumes
    the slot and leaves the MainOrchestrator execution budget reserved.
    """

    def __init__(self, original, *, max_simulation_requests):
        if (type(max_simulation_requests) is not int
                or not 1 <= max_simulation_requests <= 16):
            raise OrchestrationError("external grant Simulation limit is invalid")
        self._original = original
        self.limit = max_simulation_requests
        self.attempted = 0

    @property
    def alpha_lookup(self):
        return self._original.alpha_lookup

    @alpha_lookup.setter
    def alpha_lookup(self, value):
        self._original.alpha_lookup = value

    def call(self, command, **params):
        if command in _MUTATIONS:
            raise OrchestrationError("live pilot forbids Alpha/tag mutation and submission")
        if command in _SIMULATION:
            if self.attempted >= self.limit:
                raise OrchestrationError("single-Task Simulation authorization exhausted")
            # Increment *before* handing control to the platform. Failure,
            # timeout or lost ACK must not make this operation retryable.
            self.attempted += 1
        return self._original.call(command, **params)


class AdmittedCliDomainAdapter:
    """Actual model/driver dispatch using a pre-existing active CLI runtime.

    The consumer interface must be supplied by an independent deployment.
    No public CLI can manufacture a positive authority consumer. In tests
    its transport can be replaced with a deliberately scripted fake.
    """

    def __init__(self, orchestrator, runtime, *, authority_consumer):
        if (type(runtime) is not CliRuntimeContext
                or not callable(getattr(authority_consumer, "consume", None))
                or runtime.state_root != orchestrator.state_root
                or type(runtime.gateway) is not Gateway
                or runtime.gateway.read_only
                or runtime.gateway.orchestration_guarded
                or runtime.gateway.research_catalog_scope is not None
                or getattr(runtime.gateway, "is_offline_mock", False)
                or getattr(runtime.model, "is_offline_mock", False)
                or runtime.agent is None):
            raise OrchestrationError(
                "live domain bridge requires an active CLI and independent authority consumer")
        self.runner = orchestrator
        self.runtime = runtime
        self.authority_consumer = authority_consumer

    def _guard(self, workflow, request, context):
        if not isinstance(context, dict):
            raise OrchestrationError("Task callback context is required")
        task_id = context.get("task_id")
        task = self.runner.load(task_id)
        if (task["workflow"] != workflow
                or task["request"] != request
                or task["request_sha256"] != fingerprint_json(request)
                or task["status"] != "RUNNING"
                or task["steps"].get("execute") != "RUNNING"
                or task["budget"] != {"limit": 1, "used": 0, "reserved": 1}):
            raise OrchestrationError("live domain Task identity/budget changed before execute")
        root = self.runtime.state_root
        marker = marker_path(root)
        if marker.exists() or marker.is_symlink():
            raise OrchestrationError("live domain bridge forbidden in mock workspace")
        if StopSignals(root / "state").exists():
            raise OrchestrationError("live domain bridge blocked by Stop")
        lock = self.runtime.execution_lock_path
        if lock != root / "state" / "agent.lock":
            raise OrchestrationError("live domain bridge global lock identity invalid")
        return task

    def run(self, workflow, step, request, checkpoints, context=None):
        if step == "validate":
            validate_workflow_request(workflow, request)
            command_and_args(workflow, request)
            # Explicit exact scope is required for the first Regular live
            # end-to-end pilot, preventing implicit multi-Dataset exploration.
            if workflow != "regular":
                raise OrchestrationError(
                    "first live domain integration pilot is Regular-only")
            if (not isinstance(request.get("region"), str)
                    or not request["region"]
                    or request.get("delay") != 1
                    or not isinstance(request.get("universe"), str)
                    or not request["universe"]
                    or not isinstance(request.get("dataset_ids"), list)
                    or len(request["dataset_ids"]) != 1
                    or not isinstance(request["dataset_ids"][0], str)
                    or not request["dataset_ids"][0]
                    or not isinstance(request.get("category"), str)
                    or not request["category"]):
                raise OrchestrationError(
                    "live Regular pilot requires one Dataset, explicit Region, Category, Delay 1, Universe")
            return {"workflow": workflow, "validated": True,
                    "mode": "live_domain_bridge_requires_external_one_use_grant"}
        if step == "execute":
            task = self._guard(workflow, request, context)
            command, args = command_and_args(workflow, request)
            # Consumer is expected to verify external signature, independent
            # key custody, operation/scope/Task, budget, global Stop, one-use
            # ledger and issuer/expiry BEFORE returning an ACK. No local flag
            # or report can substitute for this external decision.
            descriptor = {
                "task_id": task["task_id"],
                "workflow": workflow, "command": command,
                "request_sha256": task["request_sha256"],
                "scope": {
                    "region": request["region"],
                    "category": request["category"],
                    "delay": 1,
                    "universe": request["universe"],
                    "dataset_id": request["dataset_ids"][0],
                },
            }
            receipt = self.authority_consumer.consume(descriptor)
            if (not isinstance(receipt, dict)
                    or receipt.get("status") != "CONSUMED_SINGLE_USE"
                    or receipt.get("task_id") != task["task_id"]
                    or receipt.get("request_sha256") != task["request_sha256"]
                    or receipt.get("operation") != "regular_research_with_simulation"
                    or not isinstance(receipt.get("external_consumption_id"), str)
                    or not receipt["external_consumption_id"].strip()
                    or type(receipt.get("max_simulation_requests")) is not int
                    or not 1 <= receipt["max_simulation_requests"] <= 16):
                raise OrchestrationError(
                    "independent one-use live Simulation admission is unavailable")
            bounded = BoundedSimulationGateway(
                self.runtime.gateway,
                max_simulation_requests=receipt["max_simulation_requests"])
            # RegularAgent is already owned by the CLI; it must not retain a
            # shadow unrestricted gateway while the domain dispatcher uses
            # the admitted wrapper. Restore the original reference afterward.
            previous = self.runtime.agent.gateway
            if previous is not self.runtime.gateway:
                raise OrchestrationError("CLI Agent Gateway identity drift")
            self.runtime.agent.gateway = bounded
            try:
                outcome = dispatch(
                    command, args, self.runtime.config,
                    self.runtime.state_root, bounded,
                    self.runtime.agent, execution_context=context)
            finally:
                self.runtime.agent.gateway = previous
            if not isinstance(outcome, dict):
                raise OrchestrationError("live domain execution returned non-object")
            # Do not turn unresolved, failed or empty campaigns into a
            # COMPLETED Task; keep the only attempt reserved for recovery.
            if (not isinstance(outcome.get("completed"), list)
                    or len(outcome["completed"]) != 1
                    or outcome.get("failures")
                    or outcome.get("status") not in ("budget_complete", "scope_complete")):
                raise OrchestrationError(
                    "live domain did not complete exactly one admitted batch")
            return {**outcome,
                    "execution_mode": "legacy_domain_via_task_bound_cli",
                    "simulation_requests_attempted": bounded.attempted,
                    "independent_consumption_id": receipt["external_consumption_id"]}
        if step == "qualify":
            return project_existing_qualification(workflow, checkpoints)
        if step == "collect":
            return {"qualification": checkpoints.get("qualify"),
                    "pool": pool_snapshot(self.runtime.state_root, self.runtime.config)}
        raise OrchestrationError("unknown live domain step")
