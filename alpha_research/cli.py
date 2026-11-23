"""Standalone command entry point for one research batch or recovery."""
import argparse
import json
import os
import re
import sys
from types import SimpleNamespace
from alpha_research.workflows.regular.agent import ResearchAgent
from alpha_research.integrations.brain.gateway import Gateway
from alpha_research.integrations.model import ModelError, build_model_client
from alpha_research.agents.registry import (
    ModelRegistryError, bundled_registry_path, load_registry, describe_registry,
    select_registered_backends, load_role_profile, assert_live_model_routes,
)
from alpha_research.agents.inspect import inspect_roles
from alpha_research.agents.doctor import check_registered_roles
from alpha_research.agents.probe import run_model_probe, ModelProbeError
from alpha_research.agents.governance import ModelCallLedger, ModelBudgetError, validate_policy
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError
from alpha_research.knowledge import KnowledgeService
from alpha_research.state.paths import CATALOG_SNAPSHOT_ROOT, default_state_root, expand_path
from alpha_research.state.store import StopSignals, update_stop_flag
from alpha_research.workflows.regular.campaign import campaign_can_resume
from alpha_research.workflows.regular.scope import ScopeDiscoveryError
from alpha_research.state.operation_lock import SubmissionGateError
from alpha_research.orchestration.dispatch import COMMANDS as RESEARCH_COMMANDS, dispatch as dispatch_research, _regular_scope, _constraint
from alpha_research.state.records import ResearchRecordStore
from alpha_research.qualification.pool import CandidatePool, policy_hash
from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.offline_adapter import OfflineResearchAdapter
from alpha_research.orchestration.prepared_mock import build_prepared_mock_adapter
from alpha_research.orchestration.domain_mock import build_domain_mock_adapter, mock_domain_boundary
from alpha_research.orchestration.mock_workspace import claim_mock_workspace, inspect_mock_workspace
from alpha_research.orchestration.admission_audit import audit_execution_admission
from alpha_research.orchestration.execution_authorization import inspect_execution_receipt
from alpha_research.orchestration.execution_reservation import reserve_non_executable_receipt
from alpha_research.orchestration.simulation_scope import inspect_simulation_scope
from alpha_research.orchestration.simulation_attempt_admission import (
    stage_simulation_attempt, inspect_staged_simulation_attempt,
)
from alpha_research.orchestration.simulation_dispatch_preflight import inspect_simulation_dispatch_gate
from alpha_research.orchestration.operator_consent import (
    inspect_consent_reservation, verify_external_consent,
    reserve_consent_for_audit,
)
from alpha_research.orchestration.authorization_handoff import (
    build_scope_issuer_request, build_operator_consent_issuer_request,
)
from alpha_research.orchestration.manifest_builder import (
    inspect_generated_manifest, read_generated_manifest,
)
from alpha_research.orchestration.research_preparation import (
    prepare_offline_research, inspect_research_preparation,
)
from alpha_research.orchestration.research_only_admission import (
    build_research_issuer_request, inspect_research_live_readiness,
)
from alpha_research.orchestration.research_grant_audit import (
    inspect_research_grant_claim, reserve_research_grant_audit,
)
from alpha_research.orchestration.research_catalog_protocol import (
    inspect_catalog_read_contract,
)
from alpha_research.orchestration.research_runtime_adapter import (
    inspect_runtime_attempt,
)
from alpha_research.orchestration.research_runtime_plan import (
    inspect_research_runtime_plan,
)
from alpha_research.orchestration.research_control_plane import (
    inspect_research_control_plane,
)
from alpha_research.orchestration.pre_dispatch_escrow import (
    inspect_pre_dispatch_escrow, reserve_pre_dispatch_escrow,
)
from alpha_research.orchestration.simulation_recovery_audit import inspect_simulation_recovery_evidence
from alpha_research.orchestration.release_gate import inspect_production_release_gate
from alpha_research.orchestration.mock_workspace import marker_path
from alpha_research.orchestration.runtime_config import (
    prepare_registered_model_runtime, load_preparation_config,
)
from alpha_research.orchestration.runtime_context import CliRuntimeContext
from alpha_research.orchestration.recovery_preflight import inspect_task_recovery_preflight
from alpha_research.orchestration.execution_proposal import persist_execution_proposal
from alpha_research.orchestration.execution_identity import (
    persist_execution_identity, inspect_domain_research_handoff,
)
from alpha_research.orchestration.recovery import RecoveryCoordinator
from alpha_research.config import DEFAULT_CONFIG
from alpha_research.preflight import local_preflight
from alpha_research.orchestration.readonly_probe import run_readonly_probe
from alpha_research.orchestration.platform_read_evidence import (
    record_platform_read_evidence, inspect_platform_read_evidence,
)
from alpha_research.progress import write_progress
from alpha_research.cli_parser import build_parser, _path_argument
from alpha_research.cli_orchestration import (
    ORCHESTRATION_COMMANDS, run_orchestration_command,
    _resolve_simulation_manifest,
)
from alpha_research.cli_status import run_status_command


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    root = args.state_root.resolve()
    stop_flag = root / "state" / "stop.flag"
    # A mock workspace is permanently excluded from every existing live
    # workflow entrypoint; the marker is not a dispatch authorization.
    live_commands = {
        "run-once", "run", "run-campaign", "run-super", "run-optimization",
        "plan-super", "plan-regular", "plan-optimization",
        "recover", "recover-research", "recover-workflow", "recover-task",
        "sync-backtest-cache", "tag-alpha",
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
    if args.command == "brain-read-evidence-status":
        result = inspect_platform_read_evidence(root, args.record_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["local_integrity_verified"] else 2
    if args.command == "brain-readonly-check":
        # This command is separate from domain Dispatch and never constructs
        # a model client, research agent or simulation authorization.
        if not args.allow_brain_read:
            print("BRAIN read-only check requires --allow-brain-read",
                  file=sys.stderr)
            return 2
        if marker_path(root).exists() or marker_path(root).is_symlink():
            print("BRAIN read-only check forbidden in mock workspace",
                  file=sys.stderr)
            return 2
        credential_location = os.environ.get("BRAIN_CREDENTIALS_FILE")
        if not credential_location and args.config:
            try:
                config = json.loads(args.config.read_text(encoding="utf-8"))
                variable = (config.get("auth") or {}).get(
                    "credential_env", "BRAIN_CREDENTIALS_FILE")
                credential_location = os.environ.get(variable)
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        if not credential_location:
            print("BRAIN credentials file environment variable is required",
                  file=sys.stderr)
            return 2
        gateway = Gateway(read_only=True)
        previous_credentials = os.environ.get("BRAIN_CREDENTIALS_FILE")
        try:
            os.environ["BRAIN_CREDENTIALS_FILE"] = str(
                expand_path(credential_location).resolve())
            gateway.start()
            result = run_readonly_probe(
                gateway, region=args.region, delay=args.delay,
                universe=args.universe)
            if args.record_evidence and result["status"] == "READ_ONLY_SCOPE_VERIFIED":
                receipt = record_platform_read_evidence(root, result)
                result = {**result, "evidence_record_id": receipt["record_id"],
                          "evidence_status": receipt["status"]}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["status"] == "READ_ONLY_SCOPE_VERIFIED" else 2
        except Exception as exc:
            # Never echo provider errors, account data, credential filenames.
            print("BRAIN read-only check blocked: " + type(exc).__name__,
                  file=sys.stderr)
            return 2
        finally:
            try:
                gateway.shutdown()
            except Exception:
                pass
            if previous_credentials is None:
                os.environ.pop("BRAIN_CREDENTIALS_FILE", None)
            else:
                os.environ["BRAIN_CREDENTIALS_FILE"] = previous_credentials
    if args.command == "preflight":
        config_path = args.config or root / "config" / "nastu_quant.json"
        report = local_preflight(
            config_path, registry_path, lead=args.lead, executor=args.executor,
            region=args.region)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "READY_LOCAL" else 2
    if args.command == "models":
        try:
            if args.models_action in {"budget", "acknowledge-unknown"}:
                if not args.research_id:
                    raise ModelBudgetError("model budget action requires --research-id")
                config_path = args.config or root / "config" / "nastu_quant.json"
                config = json.loads(config_path.read_text(encoding="utf-8"))
                if (not isinstance(config, dict)
                        or not isinstance(config.get("model_governance"), dict)):
                    raise ModelBudgetError(
                        "models budget requires a config with model_governance")
                ledger = ModelCallLedger(root / "state", config["model_governance"])
                if args.models_action == "acknowledge-unknown":
                    if not args.call_id or not args.reason:
                        raise ModelBudgetError(
                            "acknowledge-unknown requires --call-id and --reason")
                    ledger.acknowledge_unknown(
                        args.research_id, args.call_id, reason=args.reason)
                result = ledger.status(args.research_id)
            else:
                registered = load_registry(registry_path)
                if args.models_action == "list":
                    result = describe_registry(registered)
                else:
                    path = args.config or root / "config" / "nastu_quant.json"
                    config = (json.loads(path.read_text(encoding="utf-8"))
                              if path.exists() else None)
                    profile = load_role_profile(None if registry_assignments else config)
                    effective = select_registered_backends(
                        profile, registered, assignments=registry_assignments)
                    if args.models_action == "inspect":
                        result = inspect_roles(effective, [])
                        result["selected_registrations"] = registry_assignments
                        result["registry_path"] = str(registry_path)
                    elif args.models_action == "smoke":
                        if args.role is None:
                            raise ModelProbeError(
                                "models smoke requires --role research_lead, research_executor or both")
                        roles = (("research_lead", "research_executor")
                                 if args.role == "both" else (args.role,))
                        if args.allow_model_call:
                            # An authorized remote smoke must never be a mock
                            # fixture disguised as a successful real call.
                            assert_live_model_routes(effective)
                            ready = check_registered_roles(effective)
                            if any(not ready["roles"][role]["ready_local"]
                                   for role in roles):
                                raise ModelProbeError(
                                    "selected model has missing local prerequisites")
                        if args.role == "both":
                            reports = []
                            for role in roles:
                                observed = run_model_probe(
                                    effective, root, role=role,
                                    allow_model_call=args.allow_model_call)
                                reports.append(observed)
                                # A real contract failure must stop the sequence,
                                # not spend more tokens on the next backend.
                                if args.allow_model_call and observed["status"] != "PASS":
                                    break
                            result = {
                                "status": (
                                    "PASS" if len(reports) == 2 and
                                    all(row["status"] == "PASS" for row in reports)
                                    else "NOT_EXECUTED" if not args.allow_model_call
                                    else reports[-1]["status"]
                                ),
                                "probes": reports,
                                "provider_invoked": args.allow_model_call,
                                "brain_invoked": False,
                            }
                        else:
                            result = run_model_probe(
                                effective, root, role=args.role,
                                allow_model_call=args.allow_model_call)
                        result["selected_registrations"] = registry_assignments
                    else:
                        result = check_registered_roles(effective)
                        result["selected_registrations"] = registry_assignments
        except Exception as exc:
            # The models CLI is an operational boundary: don't leak provider
            # credential-bearing exceptions, network payloads, or raw prompts.
            if isinstance(exc, ModelRegistryError):
                print(f"model runtime failed: {exc}", file=sys.stderr)
                return 2
            if isinstance(exc, (OSError, KeyError, ValueError, TypeError, ModelError,
                                TimeoutError)):
                print(f"model runtime failed: {type(exc).__name__}", file=sys.stderr)
                return 2
            raise
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.models_action == "doctor" and result["status"] != "READY_LOCAL":
            return 2
        if args.models_action == "smoke" and result["status"] == "CONTRACT_MISMATCH":
            return 2
        return 0
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
            cfg = (json.loads(config_path.read_text(encoding="utf-8"))
                   if config_path.exists() else DEFAULT_CONFIG)
            if not isinstance(cfg, dict):
                raise ValueError("configuration must be an object")
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
    if args.command == "evidence-validity":
        try:
            cfg_path = args.config or root / "config" / "nastu_quant.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
            with SingleWriterLock(root / "state" / "agent.lock"):
                service = KnowledgeService(root / "state", root / "runs", cfg)
                result = service.set_evidence_validity(
                    args.evidence_id, args.status, args.reason, args.operator)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except Exception as exc:
            print(f"evidence update rejected: {exc}", file=sys.stderr)
            return 1
    if args.command == "knowledge":
        try:
            cfg_path = args.config or root / "config" / "nastu_quant.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
            with SingleWriterLock(root / "state" / "agent.lock"):
                service = KnowledgeService(root / "state", root / "runs", cfg)
                if args.knowledge_command == "list":
                    result = service.list_managed(args.kind)
                elif args.knowledge_command == "show":
                    result = service.get_managed(args.kind, args.id)
                elif args.knowledge_command == "note":
                    content = args.content_file.read_text(encoding="utf-8-sig")
                    result = service.put_note(
                        args.kind, args.key, content, args.source, date=args.date,
                        validity=args.validity, replace=args.replace, reason=args.reason,
                        operator=args.operator)
                elif args.knowledge_command == "remedy":
                    if args.remedy_command == "add":
                        payload = json.loads(args.file.read_text(encoding="utf-8-sig"))
                        result = service.add_remedy(payload, operator=args.operator)
                    else:
                        result = service.review_remedy(
                            args.id, args.status, reviewer=args.reviewer,
                            reason=args.reason)
                else:
                    tags = {"dataset_id": args.dataset,
                            "mechanism": args.mechanism,
                            "failure_signature": args.failure_signature,
                            "field_generation": args.field_generation,
                            "scope": {"region": args.region, "delay": args.delay,
                                      "universe": args.universe}}
                    result = service.search_evidence(
                        tags, k=args.limit, query_text=args.text,
                        include_history=args.include_history)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except Exception as exc:
            print(f"knowledge operation rejected: {exc}", file=sys.stderr)
            return 1
    stop_signals = StopSignals(root / "state")
    if stop_signals.exists() and args.command not in {
            "recover", "recover-research", "recover-workflow", "recover-task",
            "sync-backtest-cache", "tag-alpha"}:
        write_progress("研究已暂停，请先解除停止标记")
        print("research is stopped; no new platform work will start", file=sys.stderr)
        return 2
    config_path = args.config or root / "config" / "nastu_quant.json"
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("research config must be a JSON object")
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
        agent = SimpleNamespace(model=model)
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
