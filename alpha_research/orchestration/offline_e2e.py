"""One-shot, fully isolated offline orchestration acceptance.

This command runs the actual Task state machine and domain dispatcher with
scripted in-process Gateway/Model boundaries. In Regular, the Campaign
scheduler is real but its individual research batch is scripted. It never
performs a BRAIN simulation, Alpha submission or paid model call.

Every invocation requires a new empty, explicitly non-default state root.
The workspace is permanently marked as mock-only before any task is created.
"""
from pathlib import Path

from alpha_research.config import is_default_config_path
from alpha_research.orchestration.dispatch import _constraint
from alpha_research.orchestration.domain_mock import (
    build_domain_mock_adapter, mock_domain_boundary,
)
from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError, STEPS
from alpha_research.orchestration.execution_identity import persist_execution_identity
from alpha_research.orchestration.execution_proposal import persist_execution_proposal
from alpha_research.orchestration.mock_workspace import (
    claim_mock_workspace, inspect_mock_workspace,
)
from alpha_research.orchestration.runtime_config import (
    load_preparation_config, prepare_registered_model_runtime,
)
from alpha_research.orchestration.workflow_contract import validate_workflow_request
from alpha_research.state.paths import default_state_root
from alpha_research.state.store import atomic_write_json
from alpha_research.state.writer_lock import SingleWriterLock


def _build_request(args, config):
    scope = config["scope"]
    region = _constraint(args.region, scope.get("region"), "Region")
    delay = _constraint(args.delay, scope.get("delay"), "Delay")
    universe = _constraint(args.universe, scope.get("universe"), "Universe")
    if not isinstance(region, str) or not region.strip():
        raise OrchestrationError("offline E2E requires Region")
    if delay is None:
        delay = 1
    if universe is None:
        universe = "TOP3000"
    if type(delay) is not int or delay != 1 or universe != "TOP3000":
        raise OrchestrationError(
            "scripted offline E2E supports Delay 1 and TOP3000 only")
    request = {
        "objective": args.objective, "region": region,
        "delay": delay, "universe": universe,
    }
    if args.workflow == "regular":
        if args.alpha_id is not None:
            raise OrchestrationError("source Alpha ID only applies to optimization")
        fixed_dataset = scope.get("dataset_id")
        datasets = args.dataset or ([fixed_dataset] if fixed_dataset else None)
        if (fixed_dataset and datasets != [fixed_dataset]):
            raise OrchestrationError("requested Datasets conflict with configured scope")
        if datasets is not None and (
                len(datasets) != 1
                or not isinstance(datasets[0], str)
                or not datasets[0].strip()
                or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                       for ch in datasets[0])):
            raise OrchestrationError("offline E2E requires at most one valid Dataset ID")
        request["dataset_ids"] = datasets
        request["category"] = _constraint(
            args.category, scope.get("category"), "Category")
        request["ppa_profile"] = bool(args.ppa)
    else:
        if args.dataset or args.category is not None or args.ppa:
            raise OrchestrationError(
                "Dataset, Category and PPA apply only to Regular")
        if args.workflow == "optimization":
            if not isinstance(args.alpha_id, str) or not args.alpha_id.strip():
                raise OrchestrationError("optimization requires Alpha ID")
            request["alpha_id"] = args.alpha_id
        elif args.alpha_id is not None:
            raise OrchestrationError("source Alpha ID only applies to optimization")
    validate_workflow_request(args.workflow, request)
    return request


def run_offline_e2e(args, root, registry_assignments, registry_path):
    """Prepare and execute exactly one mock-only Task; retain readback evidence."""
    root = Path(root).resolve()
    if root == default_state_root().resolve():
        raise OrchestrationError("offline E2E requires an explicit non-default --state-root")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise OrchestrationError(
            "offline E2E requires a fresh empty --state-root; never reuse live research")
    if (set(registry_assignments) != {"research_lead", "research_executor"}
            or not all(isinstance(value, str) and value.strip()
                       for value in registry_assignments.values())):
        # Reject incomplete registrations before even creating config files.
        raise OrchestrationError("both registered Lead and Executor are required")

    config_path = args.config or root / "config" / "nastu_quant.json"
    explicit = args.config is not None and not is_default_config_path(
        config_path, root)
    config, source = load_preparation_config(config_path, explicit=explicit)
    effective = prepare_registered_model_runtime(
        config, registry_assignments, registry_path)
    request = _build_request(args, effective)

    # Even missing credentials must not result in a Gateway or model client:
    # this entrypoint only imports scripted domain ports.
    inspect_mock_workspace(root)
    with SingleWriterLock(root / "state" / "agent.lock"):
        claim_mock_workspace(root)
        runner = MainOrchestrator(root, config=effective)
        task = runner.create(args.workflow, request, budget=1)
        task_id = task["task_id"]
        persist_execution_proposal(runner, task_id)
        persist_execution_identity(
            runner, task_id, effective["model"], effective["model_governance"])
        adapter, gateway, agent = build_domain_mock_adapter(
            root, task, fault=args.fault)
        runner.adapter = adapter
        with mock_domain_boundary(request["region"]):
            done = runner.execute(task_id)

        # The Regular fake models only the batch boundary; it never invokes
        # simulate_batch. Super/Optimization simulate_super is also scripted.
        attempts = (agent.calls if args.workflow == "regular"
                    else gateway.calls.count("simulate_super"))
        completed = (
            done["status"] == "COMPLETED"
            and all(done["steps"].get(step) == "DONE" for step in STEPS)
            and done["budget"] == {"limit": 1, "used": 1, "reserved": 0}
            and done.get("research_identity") is not None
            and done.get("recovery_target") is not None
            and attempts == 1
        )
        report = {
            "schema_version": 1,
            "status": ("OFFLINE_E2E_PASSED" if completed else "OFFLINE_E2E_BLOCKED"),
            "mode": "offline_domain_mock",
            "workflow": args.workflow,
            "task_id": task_id,
            "config_source": source,
            "request_sha256": done["request_sha256"],
            "task_status": done["status"],
            "steps": done["steps"],
            "budget": done["budget"],
            "scripted_domain_attempts": attempts,
            "scripted_gateway_commands": gateway.calls,
            "research_identity_bound": done.get("research_identity") is not None,
            "recovery_target_bound": done.get("recovery_target") is not None,
            "failure": done.get("failure"),
            "automatic_retry_allowed": False,
            "provider_model_invoked": False,
            "brain_invoked": False,
            "brain_simulation_invoked": False,
            "alpha_submission_invoked": False,
            "production_authorized": False,
        }
        output = (root / "state" / "research_v1_1" / "offline_e2e"
                  / (task_id + ".json"))
        atomic_write_json(output, report)
        report["report_path"] = str(output)
        return report
