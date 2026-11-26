"""CLI boundary for research operations, recovery, local audits and opt-in IO."""
import json
import os
import sys
from types import SimpleNamespace

from alpha_research.workflows.regular.agent import ResearchAgent
from alpha_research.integrations.brain.gateway import Gateway
from alpha_research.integrations.model import ModelError, build_model_client
from alpha_research.agents.registry import (
    ModelRegistryError, bundled_registry_path, load_role_profile,
)
from alpha_research.agents.probe import run_model_probe
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError
from alpha_research.state.paths import default_state_root, expand_path
from alpha_research.state.store import StopSignals, update_stop_flag
from alpha_research.workflows.regular.scope import ScopeDiscoveryError
from alpha_research.state.operation_lock import SubmissionGateError
from alpha_research.orchestration.dispatch import (
    COMMANDS as RESEARCH_COMMANDS, dispatch as dispatch_research,
)
from alpha_research.state.records import ResearchRecordStore
from alpha_research.qualification.pool import CandidatePool, policy_hash
from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.offline_adapter import OfflineResearchAdapter
from alpha_research.orchestration.operator_consent import (
    verify_external_consent, reserve_consent_for_audit,
)
from alpha_research.orchestration.mock_workspace import marker_path
from alpha_research.orchestration.runtime_config import prepare_registered_model_runtime
from alpha_research.orchestration.runtime_context import CliRuntimeContext
from alpha_research.orchestration.recovery import RecoveryCoordinator, RecoveryError
from alpha_research.config import is_default_config_path, load_config
from alpha_research.progress import write_progress
from alpha_research.cli_parser import build_parser, _path_argument
from alpha_research.cli_orchestration import (
    ORCHESTRATION_COMMANDS, run_orchestration_command, _resolve_simulation_manifest,
)
from alpha_research.cli_status import run_status_command
from alpha_research.orchestration.execution_trace import inspect_execution_trace
from alpha_research.cli_models import run_models_command
from alpha_research.cli_probe import PROBE_COMMANDS, run_probe_command
from alpha_research.cli_knowledge import KNOWLEDGE_COMMANDS, run_knowledge_command
from alpha_research.cli_canary import CANARY_COMMANDS, run_canary_command


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    root = args.state_root.resolve()
    stop_flag = root / "state" / "stop.flag"
    # A mock workspace is permanently excluded from every existing live
    # workflow entrypoint; the marker is not a dispatch authorization.
    live_commands = {
        "run-once", "run", "run-campaign", "run-super", "run-optimization",
        "orchestration-local-pilot-regular",
        "plan-super", "plan-regular", "plan-optimization",
        "recover", "recover-research", "recover-workflow", "recover-task",
        "sync-backtest-cache", "tag-alpha",
        "canary-prepare", "canary-execute",
    }
    if args.command in live_commands and (
            marker_path(root).exists() or marker_path(root).is_symlink()):
        print("live workflow refused: offline mock workspace cannot be used for real research",
              file=sys.stderr)
        return 2
    if args.command == "models" and getattr(args, "allow_model_call", False) and (
            marker_path(root).exists() or marker_path(root).is_symlink()):
        print("live model probe refused: offline mock workspace", file=sys.stderr)
        return 2

    if args.command == "orchestration-local-pilot-regular":
        # Verify explicit opt-in and frozen Task before constructing any
        # provider, loading credentials, or opening a BRAIN session.
        if (args.allow_model_call is not True
                or args.allow_brain_simulation is not True
                or not args.lead or not args.executor):
            print("local pilot requires both registered roles and explicit "
                  "--allow-model-call --allow-brain-simulation", file=sys.stderr)
            return 2
        try:
            from alpha_research.orchestration.local_regular_pilot import _assert_single_regular
            _assert_single_regular(MainOrchestrator(root).load(args.task_id))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            print(f"local pilot preflight blocked: {exc}", file=sys.stderr)
            return 2

    registry_path = args.registry or (
        root / "config" / "model_registry.json" if
        (root / "config" / "model_registry.json").exists() else
        bundled_registry_path()
    )
    registry_assignments = {
        role: value for role, value in (
            ("research_lead", args.lead),
            ("research_executor", args.executor),
        ) if value is not None
    }
    if args.command == "orchestration-execution-trace":
        try:
            report = inspect_execution_trace(root, args.task_id)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            print(f"execution trace unavailable: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "TRACE_VERIFIED" else 2
    if args.command == "recover-task":
        try:
            task = MainOrchestrator(root).load(args.task_id)
            pilot = (task["checkpoints"].get("validate") or {}).get(
                "mode") == "supervised_local_real_pilot"
            if pilot or args.saved_review_call is not None:
                if not pilot or args.workflow != "regular" or not args.saved_review_call:
                    raise RecoveryError("Local Regular Pilot recovery requires --saved-review-call")
                from alpha_research.orchestration.saved_pilot_recovery import recover_saved_pilot
                with SingleWriterLock(root / "state" / "agent.lock"):
                    config = load_config(
                        args.config or root / "config" / "nastu_quant.json", initialize=False)
                    config = prepare_registered_model_runtime(
                        config, registry_assignments, registry_path)
                    outcome = recover_saved_pilot(root, args.task_id, args.saved_review_call, config)
                print(json.dumps(outcome, ensure_ascii=False))
                return 0 if outcome["status"] == "COMPLETED" else 2
        except (OSError, ValueError, KeyError, TypeError, ModelError, WriterLockError) as exc:
            print(f"Task recovery blocked: {exc}", file=sys.stderr)
            return 2
    if args.command == "orchestration-e2e-offline":
        # Offline-only command: never construct Gateway, Model SDK, or a
        # ResearchAgent. Its dedicated State Root is permanently mock-only.
        from alpha_research.orchestration.offline_e2e import run_offline_e2e
        try:
            report = run_offline_e2e(args, root, registry_assignments, registry_path)
        except (OSError, ValueError, TypeError, WriterLockError) as exc:
            print(f"offline E2E refused: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "OFFLINE_E2E_PASSED" else 2
    if args.command in PROBE_COMMANDS:
        return run_probe_command(args, root, registry_path,
                                 gateway_factory=Gateway)
    if args.command == "models":
        return run_models_command(
            args, root, registry_path, registry_assignments,
            model_probe=run_model_probe)
    if args.command in {"stop", "resume"}:
        try:
            update_stop_flag(stop_flag, args.command == "stop")
        except (OSError, SubmissionGateError, ValueError) as exc:
            # A symlinked stop flag is unsafe and cannot be overwritten.
            print(f"stop state not changed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"stopped": args.command == "stop"}))
        return 0
    if args.command in ORCHESTRATION_COMMANDS:
        return run_orchestration_command(
            args, root, registry_assignments, registry_path,
            verify_external_consent=verify_external_consent,
            reserve_consent_for_audit=reserve_consent_for_audit)
    if args.command in {"orchestrate-offline", "orchestration-status"}:
        try:
            with SingleWriterLock(root / "state" / "agent.lock"):
                if args.command == "orchestration-status":
                    result = MainOrchestrator(root).load(args.task_id)
                else:
                    fixtures = json.loads(args.fixtures.read_text(encoding="utf-8"))
                    runner = MainOrchestrator(root, adapter=OfflineResearchAdapter(fixtures))
                    if args.task_id:
                        result = runner.resume(args.task_id)
                    else:
                        if not args.workflow or not args.objective:
                            raise OrchestrationError("workflow and objective are required for a new task")
                        request = {"objective": args.objective}
                        if args.alpha_id:
                            request["alpha_id"] = args.alpha_id
                        task = runner.create(args.workflow, request, budget=args.budget)
                        result = runner.execute(task["task_id"])
        except (OSError, ValueError, TypeError, WriterLockError) as exc:
            print(f"offline orchestration failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command == "rebuild-pools":
        try:
            config_path = args.config or root / "config" / "nastu_quant.json"
            cfg = load_config(
                config_path,
                initialize=(args.config is None or is_default_config_path(config_path, root)))
            with SingleWriterLock(root / "state" / "agent.lock"):
                index = CandidatePool(ResearchRecordStore(root)).sync_index(
                    current_policy_sha256=policy_hash(cfg))
        except (OSError, ValueError, KeyError, TypeError, WriterLockError) as exc:
            print(f"pool rebuild failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(index, ensure_ascii=False))
        return 0
    if args.command == "status":
        return run_status_command(args, root, stop_flag)
    if args.command == "void":
        try:
            with SingleWriterLock(root / "state" / "agent.lock"):
                agent = ResearchAgent(None, None, {}, root / "runs")
                record = agent.void_batch(args.batch_dir, args.reason)
            print(json.dumps(record, ensure_ascii=False))
            return 0
        except Exception as exc:
            print(f"void rejected: {exc}", file=sys.stderr)
            return 1
    if args.command in KNOWLEDGE_COMMANDS:
        return run_knowledge_command(args, root)
    if args.command in CANARY_COMMANDS:
        return run_canary_command(args, root, gateway_factory=Gateway)
    stop_signals = StopSignals(root / "state")
    if stop_signals.exists() and args.command not in {
            "recover", "recover-research", "recover-workflow", "recover-task",
            "sync-backtest-cache", "tag-alpha"}:
        write_progress("研究已暂停，请先解除停止标记")
        print("research is stopped; no new platform work will start", file=sys.stderr)
        return 2
    config_path = args.config or root / "config" / "nastu_quant.json"
    try:
        cfg = load_config(
            config_path,
            initialize=(args.config is None or is_default_config_path(config_path, root)))
        # Planning, platform evidence and non-Regular recovery never invoke
        # an Agent or model. Do not require paid-model credentials, local
        # CLIs, role selections or registry availability for these commands.
        model_free = (
            args.command in {"plan-regular", "plan-optimization", "plan-super",
                             "recover-research", "sync-backtest-cache", "tag-alpha"}
            or (args.command in {"recover-workflow", "recover-task"}
                and args.workflow != "regular")
        )
        if model_free:
            model = None
        else:
            if registry_assignments:
                cfg = prepare_registered_model_runtime(
                    cfg, registry_assignments, registry_path)
            else:
                cfg["model"] = load_role_profile(cfg)
                if cfg["model"].get("provider") == "role_router":
                    raise ModelRegistryError(
                        "select registered roles with --lead <id> --executor <id>")
            model = build_model_client(cfg["model"])
    except (OSError, ValueError, KeyError, TypeError, ModelError) as exc:
        write_progress("无法加载研究配置或模型配置")
        print(f"configuration unavailable: {exc}", file=sys.stderr)
        return 2
    os.environ["NASTU_QUANT_STATE_DIR"] = str(root / "state")
    credential_env = (cfg.get("auth") or {}).get("credential_env", "BRAIN_CREDENTIALS_FILE")
    if not os.environ.get("BRAIN_CREDENTIALS_FILE") and credential_env:
        configured_credentials = os.environ.get(credential_env)
        if configured_credentials:
            os.environ["BRAIN_CREDENTIALS_FILE"] = str(
                expand_path(configured_credentials).resolve())
    if getattr(args, "brain_credentials_stdin", False):
        try:
            credentials = json.loads(sys.stdin.readline(16385))
            if not isinstance(credentials, dict):
                raise ValueError("BRAIN credentials input is invalid")
        except (OSError, ValueError):
            write_progress("无法读取工作台提供的 BRAIN 凭证")
            print("BRAIN credentials input is unavailable or invalid", file=sys.stderr)
            return 2
        gateway = Gateway(credentials=credentials)
    else:
        gateway = Gateway()
    agent = None
    if args.command in {"run-super", "run-optimization"}:
        agent = SimpleNamespace(model=model, stop_flag=stop_signals)
    elif args.command not in {"plan-regular", "plan-optimization", "plan-super", "recover-research",
                              "sync-backtest-cache", "tag-alpha"} and not (
            args.command in {"recover-workflow", "recover-task"} and args.workflow != "regular"):
        agent = ResearchAgent(gateway, model, cfg, root / "runs", stop_flag=stop_signals)
    lock = SingleWriterLock(root / "state" / "agent.lock")
    try:
        lock.__enter__()
    except WriterLockError as exc:
        write_progress("无法获取执行锁，请检查是否已有研究进程")
        print(f"research paused: {exc}", file=sys.stderr)
        return 2
    try:
        write_progress("正在启动 BRAIN 连接")
        gateway.start()
        runtime = CliRuntimeContext.from_active_cli(
            root, cfg, gateway, model, agent, lock)
        if args.command == "sync-backtest-cache":
            print(json.dumps(gateway.call("sync_backtest_cache"), ensure_ascii=False))
            return 0
        if args.command == "tag-alpha":
            result = gateway.call("tag_alpha", alpha_id=args.alpha_id,
                                  apply=args.apply, diagnostic=args.diagnostic)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "recover-workflow":
            result = RecoveryCoordinator(root).recover(
                args.target, gateway=gateway, agent=agent, config=cfg,
                expected_workflow=args.workflow)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "recover-task":
            result = RecoveryCoordinator(root).recover_task(
                MainOrchestrator(root), args.task_id, gateway=gateway,
                agent=agent, config=cfg, expected_workflow=args.workflow)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "orchestration-local-pilot-regular":
            from alpha_research.orchestration.local_regular_pilot import run_local_regular_pilot
            result = run_local_regular_pilot(
                root, runtime, args.task_id,
                model_config=cfg["model"],
                governance_policy=cfg["model_governance"])
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result.get("status") == "COMPLETED" else 2
        if args.command in RESEARCH_COMMANDS:
            result = runtime.dispatch(args.command, args, dispatcher=dispatch_research)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.command == "run-once":
            result = agent.run_once(
                args.objective, dataset_id=args.dataset,
                diagnosis_id=args.diagnosis_id, remedy_id=args.remedy_id,
                allow_remedy_round_overrun=args.allow_remedy_round_overrun)
        elif args.command == "run":
            result = agent.run_case(args.objective, args.max_batches,
                                    dataset_id=args.dataset,
                                    diagnosis_id=args.diagnosis_id, remedy_id=args.remedy_id,
                                    allow_remedy_round_overrun=args.allow_remedy_round_overrun)
        else:
            result = RecoveryCoordinator(root).recover(
                args.batch_dir, gateway=gateway, agent=agent, config=cfg)
        if args.command == "run":
            print(json.dumps(result, ensure_ascii=False))
        else:
            summary = {"dataset_id": result["dataset_id"],
                       "alpha_ids": result["alpha_ids"],
                       "candidate_status": result["candidate_status"]}
            ppa = {alpha_id: verdict["ppa_eligibility"]
                   for alpha_id, verdict in result.get("gate", {}).items()
                   if "ppa_eligibility" in verdict}
            if ppa:
                summary["ppa_eligibility"] = ppa
            print(json.dumps(summary, ensure_ascii=False))
        return 0
    except Exception as exc:
        if isinstance(exc, ScopeDiscoveryError):
            write_progress(f"研究范围核验失败 · {exc}")
        else:
            write_progress(f"研究已暂停，异常类型：{type(exc).__name__}")
        print(f"research paused: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            gateway.shutdown()
        except Exception:
            pass
        lock.__exit__(None, None, None)


if __name__ == "__main__":
    sys.exit(main())
