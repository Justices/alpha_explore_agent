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
                not isinstance(datasets, list) or not datasets
                or any(not isinstance(dataset_id, str) or not dataset_id.strip()
                       or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                              for ch in dataset_id)
                       for dataset_id in datasets)
                or len(set(datasets)) != len(datasets)):
            raise OrchestrationError(
                "offline E2E requires a nonempty list of unique valid Dataset IDs")
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


def _verify_domain_handoffs(trace, workflow, expected_attempts):
    """Audit a completed offline vertical slice using existing domain events.

    A valid Task hash chain alone is insufficient: the domain must have
    emitted a complete, ordered execution chain and its stored evidence must
    match the expected workflow-specific handoffs. This is read-only.
    """
    events = [row.get("event_type") for row in trace.get("events", [])
              if isinstance(row, dict)]
    required = {
        "regular": (
            "regular.campaign_scheduled", "regular.batch_started",
            "regular.batch_completed", "regular.dataset_review_started",
            "regular.dataset_attribution_started",
            "regular.dataset_attribution_completed", "regular.dataset_no_selection",
            "regular.dataset_review_completed", "regular.campaign_completed"),
        "super": (
            "super.research_started", "super.proposal_generated",
            "super.proposal_validated", "super.simulation_intent_prepared",
            "super.simulation_readback_completed", "super.qualification_completed"),
        "optimization": (
            "optimization.research_started", "optimization.source_snapshot_captured",
            "optimization.proposal_generated", "optimization.candidate_branch_created",
            "optimization.simulation_intent_prepared",
            "optimization.simulation_readback_completed",
            "optimization.source_integrity_verified",
            "optimization.qualification_completed"),
    }[workflow]
    missing = [name for name in required if name not in events]
    positions = [events.index(name) for name in required if name in events]
    ordered = not missing and positions == sorted(positions)
    domain = trace.get("domain_evidence") or {}
    matching = lambda name: [row.get("payload") or {} for row in trace.get("events", [])
                             if isinstance(row, dict) and row.get("event_type") == name]
    if workflow == "regular":
        campaign = domain.get("campaign") or {}
        completed = campaign.get("completed") or []
        dataset_ids = {row.get("dataset_id") for row in completed}
        review_rows = campaign.get("dataset_reviews") or {}
        review_refs = {row.get("review_ref") for row in review_rows.values()
                       if isinstance(row, dict)}
        evidence_files = (trace.get("evidence_manifest") or {}).get("files", [])
        evidence_paths = {row.get("path") for row in evidence_files
                          if isinstance(row, dict)}
        review_evidence_bound = (
            len(review_refs) == expected_attempts
            and None not in review_refs
            and review_refs.issubset(evidence_paths))
        regular_stages = (
            "regular.batch_started", "regular.batch_completed",
            "regular.dataset_review_started",
            "regular.dataset_attribution_started",
            "regular.dataset_attribution_completed",
            "regular.dataset_no_selection", "regular.dataset_review_completed",
        )
        domain_valid = (
            len(completed) == expected_attempts
            and len(dataset_ids) == expected_attempts
            and all(events.count(name) == expected_attempts
                    for name in regular_stages)
            and len(review_rows) == expected_attempts
            and all(row.get("status") == "no_selection"
                    for row in review_rows.values())
            and review_evidence_bound
            and isinstance(campaign.get("campaign_id"), str)
            and bool(campaign.get("campaign_id"))
            and all({row.get("dataset_id") for row in matching(name)} == dataset_ids
                    for name in regular_stages)
            and all(row.get("campaign_id") == campaign["campaign_id"]
                    for name in regular_stages for row in matching(name)))
    elif workflow == "super":
        run = domain.get("super_run") or {}
        domain_valid = all(run.get(key) for key in (
            "run_id", "simulation_intent_ref", "readback_ref",
            "candidate_id", "candidate_status", "preferred_status")) and all(
                row.get("run_id") == run["run_id"] and
                row.get("candidate_id") == run["candidate_id"]
                for row in matching("super.qualification_completed"))
    else:
        optimization = domain.get("optimization") or {}
        report = optimization.get("report") or {}
        domain_valid = all(report.get(key) for key in (
            "source_snapshot_ref", "simulation_intent_ref", "readback_ref",
            "candidate_id", "candidate_record_ref", "preferred_record_ref",
        )) and report.get("source_unchanged_verified") is True and all(
            row.get("candidate_id") == report["candidate_id"] and
            row.get("run_id") == optimization.get("run_id")
            for row in matching("optimization.qualification_completed"))
    return {
        "status": "VERIFIED" if ordered and domain_valid else "INCOMPLETE",
        "missing_events": missing,
        "events_ordered": ordered,
        "domain_evidence_bound": bool(domain_valid),
        "review_evidence_bound": (review_evidence_bound
                                  if workflow == "regular" else None),
    }


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

    # An external --config may be a user's live research file. Canonical
    # config loading can migrate it in place, which violates mock isolation.
    # Only the new workspace's own default config may be initialized here.
    config_path = args.config or root / "config" / "nastu_quant.json"
    if not is_default_config_path(config_path, root):
        raise OrchestrationError(
            "offline E2E accepts only its State Root default config path")
    config, source = load_preparation_config(config_path, explicit=False)
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
        expected_attempts = (
            len(request.get("dataset_ids") or ["OFFLINE_DATASET"])
            if args.workflow == "regular" else 1)
        execution = done.get("checkpoints", {}).get("execute") or {}
        completed_batches = execution.get("completed", [])
        completed = (
            done["status"] == "COMPLETED"
            and all(done["steps"].get(step) == "DONE" for step in STEPS)
            and done["budget"] == {"limit": 1, "used": 1, "reserved": 0}
            and done.get("research_identity") is not None
            and done.get("recovery_target") is not None
            and attempts == expected_attempts
            and (args.workflow != "regular"
                 or isinstance(completed_batches, list)
                 and len(completed_batches) == expected_attempts
                 and {row.get("dataset_id") for row in completed_batches}
                     == set(request.get("dataset_ids") or ["OFFLINE_DATASET"]))
        )
        from alpha_research.orchestration.execution_trace import inspect_execution_trace
        trace = inspect_execution_trace(root, task_id)
        trace_verified = trace["status"] == "TRACE_VERIFIED"
        handoffs = _verify_domain_handoffs(trace, args.workflow, expected_attempts)
        completed = completed and trace_verified and handoffs["status"] == "VERIFIED"
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
            "expected_domain_attempts": expected_attempts,
            "completed_dataset_batches": len(completed_batches)
                if args.workflow == "regular" and isinstance(completed_batches, list)
                else None,
            "requested_dataset_ids": request.get("dataset_ids"),
            "scripted_gateway_commands": gateway.calls,
            "research_identity_bound": done.get("research_identity") is not None,
            "recovery_target_bound": done.get("recovery_target") is not None,
            "failure": done.get("failure"),
            "trace_status": trace["status"],
            "workflow_handoff_audit": handoffs,
            "trace_ref": trace["journal_verification"]["journal_ref"],
            "trace_event_count": trace["journal_verification"]["event_count"],
            "trace_last_event_sha256": trace["journal_verification"]["last_event_sha256"],
            "trace_binding_valid": trace["trace_binding_valid"],
            "evidence_manifest_status": trace["evidence_manifest"]["status"],
            "evidence_file_count": trace["evidence_manifest"]["file_count"],
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
