"""CLI-facing routing for V1.1 workflows.

The CLI owns authentication and locking. Each workflow package owns research
behavior and persisted results.
"""

from pathlib import Path
from uuid import uuid4

from alpha_research.state.store import atomic_write_json
from alpha_research.workflows.regular.scope import discover_regular_scope
from alpha_research.workflows.regular.campaign import load_saved_campaign, run_regular_campaign
from alpha_research.workflows.super_alpha.workflow import prepare_super_run, run_super_research
from alpha_research.workflows.optimization.workflow import prepare_source, run_optimization
from alpha_research.orchestration.recovery import RecoveryCoordinator


COMMANDS = {"plan-regular", "run-campaign", "plan-super", "run-super", "plan-optimization", "run-optimization", "recover-research"}


def _constraint(supplied, configured, name):
    if supplied is not None and configured is not None and supplied != configured:
        raise ValueError(f"requested {name} conflicts with configured scope")
    return configured if supplied is None else supplied


def _regular_scope(args, cfg):
    configured = cfg.get("scope") or {}
    region = _constraint(args.region, configured.get("region"), "Region")
    fixed_dataset = configured.get("dataset_id")
    if fixed_dataset and args.dataset and args.dataset != [fixed_dataset]:
        raise ValueError("requested Datasets conflict with configured scope")
    return {
        "region": region,
        "dataset_ids": args.dataset or ([fixed_dataset] if fixed_dataset else None),
        "category": _constraint(args.category, configured.get("category"), "Category"),
        "delay": _constraint(args.delay, configured.get("delay"), "Delay"),
        "universe": _constraint(args.universe, configured.get("universe"), "Universe"),
    }


def dispatch(command, args, cfg, root: Path, gateway, agent=None, execution_context=None):
    if command not in COMMANDS:
        raise ValueError(f"unknown research workflow command: {command}")
    if command == "recover-research":
        return RecoveryCoordinator(root).recover(args.intent_path, gateway=gateway, config=cfg)
    if command in {"plan-optimization", "run-optimization"}:
        region = _constraint(args.region, (cfg.get("scope") or {}).get("region"),
                             "Region")
        if command == "run-optimization":
            if agent is None:
                raise ValueError("Optimization model is unavailable")
            return run_optimization(gateway, agent.model, cfg, root,
                                    alpha_id=args.alpha_id,
                                    objective=args.objective, region=region,
                                    execution_context=execution_context)
        return prepare_source(gateway, root, alpha_id=args.alpha_id,
                              objective=args.objective, region=region)
    if command in {"plan-super", "run-super"}:
        configured = cfg.get("scope") or {}
        region = _constraint(args.region, configured.get("region"), "Region")
        delay = _constraint(args.delay, configured.get("delay"), "Delay")
        if command == "run-super":
            if agent is None:
                raise ValueError("Super research model is unavailable")
            return run_super_research(gateway, agent.model, cfg, root,
                                      region=region, objective=args.objective,
                                      delay=delay, execution_context=execution_context)
        return prepare_super_run(gateway, root, region=region,
                                 objective=args.objective, delay=delay)
    if command == "run-campaign":
        if agent is None:
            raise ValueError("Regular Campaign executor is unavailable")
        resume_id = getattr(args, "resume_campaign", None)
        if resume_id:
            _, saved = load_saved_campaign(root, resume_id)
            return run_regular_campaign(agent, gateway, resume_campaign_id=resume_id,
                                        **saved["request"])
        return run_regular_campaign(
            agent, gateway, objective=args.objective, max_batches=args.max_batches,
            ppa_profile=args.ppa, execution_context=execution_context, **_regular_scope(args, cfg))
    scope = _regular_scope(args, cfg)
    plan_id = uuid4().hex
    gateway.call("set_case", case_key=plan_id, run_id=uuid4().hex)
    result = discover_regular_scope(gateway, **scope)
    result["plan_id"] = plan_id
    result["status"] = "planned" if result["coverage_complete"] else "unresolved"
    atomic_write_json(Path(root) / "state" / "research_v1_1" / "plans" /
                      f"{plan_id}.json", result)
    return result
