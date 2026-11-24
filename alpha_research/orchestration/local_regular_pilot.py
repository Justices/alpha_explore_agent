"""Supervised LOCAL Regular end-to-end pilot through the real domain workflow.

This is deliberately NOT P7 production release. It reuses the pre-existing
live Regular Campaign CLI and its domain-owned model ledger/Simulation intent
while binding outcomes to MainOrchestrator Task/checkpoints/recovery.

Only an explicitly opted-in, fresh, single-Dataset, single-batch Task may use
this path. One simulate_batch command maps to at most one platform POST (up
to the platform's eight-expression bound). A lost response remains BLOCKED;
nothing is replayed automatically. No Alpha submission/property changes.
"""
from pathlib import Path
from types import SimpleNamespace

from alpha_research.agents.registry import assert_live_model_routes
from alpha_research.orchestration.engine import (
    MainOrchestrator, OrchestrationError, pool_snapshot,
)
from alpha_research.orchestration.execution_identity import inspect_execution_identity
from alpha_research.orchestration.execution_proposal import inspect_execution_proposal
from alpha_research.orchestration.mock_workspace import marker_path
from alpha_research.orchestration.runtime_context import CliRuntimeContext
from alpha_research.orchestration.workflow_contract import (
    command_and_args, project_existing_qualification, validate_workflow_request,
)
from alpha_research.orchestration.dispatch import dispatch
from alpha_research.state.store import StopSignals

_ALLOWED_REQUEST = frozenset({
    "objective", "region", "dataset_ids", "category", "delay",
    "universe", "max_batches", "ppa_profile",
})


def _assert_single_regular(task):
    if task.get("workflow") != "regular":
        raise OrchestrationError("local real pilot supports Regular only")
    if (task.get("status") != "CREATED"
            or task.get("steps") != {
                "validate": "PENDING", "execute": "PENDING",
                "qualify": "PENDING", "collect": "PENDING"}
            or task.get("budget") != {"limit": 1, "used": 0, "reserved": 0}
            or task.get("recovery_target") is not None):
        raise OrchestrationError("pilot requires a fresh, unexecuted one-batch Task")
    request = task.get("request")
    if not isinstance(request, dict) or set(request) - _ALLOWED_REQUEST:
        raise OrchestrationError("pilot request contains unexpected fields")
    validate_workflow_request("regular", request)
    datasets = request.get("dataset_ids")
    if (not isinstance(datasets, list) or len(datasets) != 1
            or not isinstance(datasets[0], str) or not datasets[0]
            or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                   for ch in datasets[0])):
        raise OrchestrationError("pilot requires exactly one pinned Dataset ID")
    if (not isinstance(request.get("region"), str)
            or not request["region"]
            or not isinstance(request.get("universe"), str)
            or not request["universe"]
            or type(request.get("delay")) is not int
            or request["delay"] != 1):
        raise OrchestrationError("pilot requires pinned Region/Universe and Delay 1")
    if (type(request.get("max_batches", 1)) is not int
            or request.get("max_batches", 1) != 1):
        raise OrchestrationError("pilot permits one research batch only")
    if type(request.get("ppa_profile", False)) is not bool:
        raise OrchestrationError("pilot PPA selector must be boolean")
    return request


class _SinglePostGateway:
    """Narrow command wrapper; no new Gateway/session/driver is constructed."""

    def __init__(self, gateway, root):
        self._gateway = gateway
        self._root = Path(root).resolve()
        self.simulation_commands_reserved = 0
        self.is_offline_mock = False

    @property
    def alpha_lookup(self):
        return getattr(self._gateway, "alpha_lookup", None)

    @alpha_lookup.setter
    def alpha_lookup(self, value):
        self._gateway.alpha_lookup = value

    def call(self, command, **params):
        if command in {"simulate_super", "simulate_canary_once",
                       "submit_alpha", "tag_alpha", "set_alpha_properties"}:
            raise OrchestrationError(
                "local Regular pilot forbids submission, property changes and Super")
        if command == "simulate_batch":
            if self.simulation_commands_reserved:
                raise OrchestrationError("pilot one-post limit reached; never repost")
            if (params.get("label") != "production"
                    or type(params.get("variants")) is not list
                    or not 1 <= len(params["variants"]) <= 8):
                raise OrchestrationError(
                    "pilot requires one production batch of 1-8 expressions")
            if StopSignals(self._root / "state").exists():
                raise OrchestrationError("pilot stopped before Simulation")
            # Reserve BEFORE transport: a timeout must not authorize a retry.
            self.simulation_commands_reserved += 1
        return self._gateway.call(command, **params)


def validate_local_pilot_task(runner, task_id, model_config, governance_policy):
    """Read-only identity and one-batch safety check used by audit AND dispatch.

    No Gateway, model SDK, platform session or Simulation is instantiated.
    A successful return is local task eligibility, never authorization.
    """
    task = runner.load(task_id)
    request = _assert_single_regular(task)
    inspect_execution_proposal(runner, task_id)
    inspect_execution_identity(runner, task_id, model_config, governance_policy)
    assert_live_model_routes(model_config)
    marker = marker_path(runner.state_root)
    if marker.exists() or marker.is_symlink():
        raise OrchestrationError("pilot forbidden in mock workspace")
    if StopSignals(runner.state_root / "state").exists():
        raise OrchestrationError("global stop flag active")
    from alpha_research.workflows.regular.campaign import _key
    campaign_request = {
        "objective": request["objective"],
        "region": request["region"],
        "dataset_ids": request["dataset_ids"],
        "category": request.get("category"),
        "delay": request["delay"],
        "universe": request["universe"],
        "max_batches": 1,
        "ppa_profile": request.get("ppa_profile", False),
    }
    campaign_id = _key({**campaign_request, "batch_budget": "per_dataset"})
    campaign_path = (runner.state_root / "state" / "research_v1_1"
                     / "campaign_runs" / (campaign_id + ".json"))
    if campaign_path.exists() or campaign_path.is_symlink():
        raise OrchestrationError(
            "pilot Campaign already exists; reconcile it rather than repost")
    return request


class LocalRegularPilotAdapter:
    """Task-bound adapter; only created inside explicitly opted-in active CLI."""

    def __init__(self, runner, runtime, task_id, model_config, governance_policy):
        if type(runtime) is not CliRuntimeContext:
            raise OrchestrationError("pilot requires existing CLI runtime context")
        if Path(runtime.state_root).resolve() != runner.state_root:
            raise OrchestrationError("pilot runtime state-root identity mismatch")
        if (runtime.gateway is None or runtime.model is None
                or runtime.agent is None
                or getattr(runtime.gateway, "is_offline_mock", False)
                or getattr(runtime.model, "is_offline_mock", False)):
            raise OrchestrationError("pilot cannot run with absent or mock live providers")
        self.runner = runner
        self.runtime = runtime
        self.task_id = task_id
        self.config = runtime.config
        self.gateway = _SinglePostGateway(runtime.gateway, runner.state_root)
        self.request = validate_local_pilot_task(
            runner, task_id, model_config, governance_policy)

    def run(self, workflow, step, request, checkpoints, context=None):
        if workflow != "regular" or request != self.request:
            raise OrchestrationError("pilot workflow or frozen Task request drift")
        if step == "validate":
            return {**validate_workflow_request(workflow, request),
                    "mode": "supervised_local_real_pilot",
                    "production_release_authorized": False,
                    "max_simulation_post": 1,
                    "max_variants_per_post": 8}
        if step == "execute":
            if (not isinstance(context, dict)
                    or context.get("task_id") != self.task_id
                    or not callable(context.get("bind_research_identity"))
                    or not callable(context.get("bind_recovery_target"))):
                raise OrchestrationError("pilot missing durable Task and recovery bindings")
            if StopSignals(self.runner.state_root / "state").exists():
                raise OrchestrationError("pilot stopped before workflow dispatch")
            command, args = command_and_args("regular", request)
            agent = self.runtime.agent
            original_gateway = agent.gateway
            # The SAME active model and domain agent are reused. Both paths
            # pass through one bounded gateway; the wrapper is never persisted.
            try:
                agent.gateway = self.gateway
                # The supervised one-POST pilot reserves its only BRAIN
                # Simulation for the production batch. Catalog coverage is
                # checked deterministically; optional diagnostic probes are
                # disabled and cannot consume or collide with that reservation.
                domain_context = {
                    **context, "allow_diagnostic_simulations": False,
                }
                outcome = dispatch(
                    command, args, self.config, self.runner.state_root,
                    self.gateway, agent, execution_context=domain_context)
            finally:
                agent.gateway = original_gateway
            if not isinstance(outcome, dict):
                raise OrchestrationError("pilot domain executor returned invalid result")
            # A campaign can return an unresolved/empty result without throwing.
            # Do not mark the Task COMPLETED, spend the orchestration budget
            # and claim an end-to-end success without a real bounded simulation,
            # one completed batch, a bound Research ID and a recovery Intent.
            reloaded = self.runner.load(self.task_id)
            if (self.gateway.simulation_commands_reserved != 1
                    or outcome.get("status") not in {"scope_complete", "budget_complete"}
                    or not isinstance(outcome.get("completed"), list)
                    or len(outcome["completed"]) != 1
                    or outcome.get("failures")
                    or not isinstance(reloaded.get("research_identity"), dict)
                    or not isinstance(reloaded.get("recovery_target"), dict)):
                raise OrchestrationError(
                    "pilot end-to-end evidence incomplete; inspect original Task/Intent before retry")
            return {**outcome, "mode": "supervised_local_real_pilot",
                    "simulation_commands_reserved":
                    self.gateway.simulation_commands_reserved,
                    "production_release_authorized": False}
        if step == "qualify":
            return project_existing_qualification(workflow, checkpoints)
        if step == "collect":
            return {
                "qualification": checkpoints.get("qualify"),
                "pool": pool_snapshot(self.runner.state_root, self.config),
                "mode": "supervised_local_real_pilot",
                "production_release_authorized": False,
            }
        raise OrchestrationError("unknown pilot orchestration step")


def run_local_regular_pilot(root, runtime, task_id, *, model_config, governance_policy):
    """All external IO happens only after CLI-scoped explicit opt-in and lock."""
    runner = MainOrchestrator(root, config=runtime.config)
    adapter = LocalRegularPilotAdapter(
        runner, runtime, task_id, model_config, governance_policy)
    runner.adapter = adapter
    return runner.execute(task_id)
