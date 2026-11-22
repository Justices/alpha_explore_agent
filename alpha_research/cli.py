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


def _path_argument(value):
    return expand_path(value)


def _resolve_simulation_manifest(orchestrator, task_id, manifest_path):
    """Auto-load the Agent snapshot; legacy overrides may not drift from it."""
    generated = read_generated_manifest(orchestrator, task_id)
    if manifest_path is None:
        return generated
    candidate = json.loads(manifest_path.read_text(encoding="utf-8"))
    if generated is not None and candidate != generated:
        raise OrchestrationError(
            "external manifest conflicts with Agent-pinned Simulation snapshot")
    return candidate


def main(argv=None):
    parser = argparse.ArgumentParser(prog="alpha_research")
    parser.add_argument("--state-root", type=_path_argument, default=default_state_root())
    parser.add_argument("--config", type=_path_argument)
    parser.add_argument("--registry", type=_path_argument,
                        help="model/agent registration JSON; defaults to bundled preset when selecting")
    parser.add_argument("--lead", metavar="ID",
                        help="registered backend ID for research_lead")
    parser.add_argument("--executor", metavar="ID",
                        help="registered backend ID for research_executor")
    sub = parser.add_subparsers(dest="command", required=True)
    readiness = sub.add_parser(
        "preflight", help="check local research/model/BRAIN prerequisites without external calls")
    readiness.add_argument("--region",
                           help="planned research Region; conflicts with fixed scope are blocked")
    brain_read = sub.add_parser(
        "brain-readonly-check",
        help="explicit BRAIN read-only OPTIONS/GET connectivity and Region catalog check")
    brain_read.add_argument("--region", required=True)
    brain_read.add_argument("--delay", type=int, choices=(0, 1), required=True)
    brain_read.add_argument("--universe", required=True)
    brain_read.add_argument("--allow-brain-read", action="store_true",
                            help="authorize authentication and two read-only BRAIN calls")
    brain_read.add_argument("--record-evidence", action="store_true",
                            help="append local read result evidence; NO live authorization")
    read_evidence = sub.add_parser(
        "brain-read-evidence-status",
        help="inspect local read probe evidence; never contact BRAIN")
    read_evidence.add_argument("record_id")
    models = sub.add_parser(
        "models", help="inspect registered models or explicitly probe one model")
    models.add_argument("models_action", choices=(
        "list", "inspect", "doctor", "budget", "acknowledge-unknown", "smoke"))
    models.add_argument("--research-id", help="research ID for budget/acknowledgement")
    models.add_argument("--call-id", help="unresolved model call ID to acknowledge")
    models.add_argument("--reason", help="human explanation for unknown outcome")
    models.add_argument("--role", choices=("research_lead", "research_executor", "both"),
                        help="one role or both sequentially with models smoke")
    models.add_argument("--allow-model-call", action="store_true",
                        help="explicit authorization for one paid model/API invocation")
    orchestrate = sub.add_parser("orchestrate-offline", help="run or resume fixture-only research orchestration")
    orchestrate.add_argument("--workflow", choices=("regular", "super", "optimization"))
    orchestrate.add_argument("--objective")
    orchestrate.add_argument("--alpha-id")
    orchestrate.add_argument("--task-id")
    orchestrate.add_argument("--fixtures", type=_path_argument, required=True)
    orchestrate.add_argument("--budget", type=int, default=1)
    prepare = sub.add_parser(
        "orchestration-prepare",
        help="create a one-batch task and immutable model identity without model/BRAIN calls")
    prepare.add_argument("--workflow", choices=("regular", "super", "optimization"),
                         required=True)
    prepare.add_argument("--objective", required=True)
    prepare.add_argument("--alpha-id")
    prepare.add_argument("--region")
    prepare.add_argument("--delay", type=int)
    prepare.add_argument("--dataset", action="append")
    prepare.add_argument("--category")
    prepare.add_argument("--universe")
    prepare.add_argument("--ppa", action="store_true")
    mock_run = sub.add_parser(
        "orchestration-run",
        help="run a prepared task with built-in synthetic fixtures only; no model/BRAIN")
    mock_run.add_argument("task_id")
    mock_run.add_argument("--mode", choices=("mock",), required=True)
    domain_mock = sub.add_parser(
        "orchestration-domain-mock",
        help="run Super/Optimization domain workflow using scripted offline Gateway/Model")
    domain_mock.add_argument("task_id")
    domain_mock.add_argument("--mode", choices=("mock",), required=True)
    domain_mock.add_argument("--fault", choices=("lost-simulation-response",))
    admission = sub.add_parser(
        "orchestration-admission-audit",
        help="inspect production authorization blockers without provider or BRAIN calls")
    admission.add_argument("task_id")
    authorization = sub.add_parser(
        "orchestration-authorization-inspect",
        help="verify a signed execution receipt without granting live execution")
    authorization.add_argument("task_id")
    authorization.add_argument("--receipt", required=True, type=_path_argument)
    reserve_receipt = sub.add_parser(
        "orchestration-authorization-reserve",
        help="reserve signed execution receipt once for audit ONLY; never execute")
    reserve_receipt.add_argument("task_id")
    reserve_receipt.add_argument("--receipt", required=True, type=_path_argument)
    simulation_scope = sub.add_parser(
        "orchestration-simulation-scope-check",
        help="verify external Simulation scope receipt and manifest; NEVER dispatch")
    simulation_scope.add_argument("task_id")
    simulation_scope.add_argument("--receipt", required=True, type=_path_argument)
    simulation_scope.add_argument(
        "--manifest", type=_path_argument,
        help="optional external manifest, otherwise use internal Agent snapshot")
    sim_stage = sub.add_parser(
        "orchestration-simulation-stage",
        help="atomically stage one signed Simulation intent; NO BRAIN dispatch")
    sim_stage.add_argument("task_id")
    sim_stage.add_argument("--receipt", required=True, type=_path_argument)
    sim_stage.add_argument(
        "--manifest", type=_path_argument,
        help="optional external manifest, otherwise use internal Agent snapshot")
    sim_stage_status = sub.add_parser(
        "orchestration-simulation-stage-status",
        help="read-only inspection of staged Simulation attempt")
    sim_stage_status.add_argument("task_id")
    consent_status = sub.add_parser(
        "orchestration-consent-status",
        help="read-only one-shot operator consent audit status; NEVER grant execution")
    consent_status.add_argument("task_id")
    scope_handoff = sub.add_parser(
        "orchestration-scope-issuer-request",
        help="read-only scope-signature request; does NOT sign or authorize")
    scope_handoff.add_argument("task_id")
    scope_handoff.add_argument(
        "--manifest", type=_path_argument,
        help="optional reviewed external manifest; default is Agent-produced snapshot")
    manifest_status = sub.add_parser(
        "orchestration-manifest-status",
        help="read-only inspection of Agent-produced Simulation snapshot")
    manifest_status.add_argument("task_id")
    consent_handoff = sub.add_parser(
        "orchestration-consent-issuer-request",
        help="read-only operator approval handoff; requires staged Intent")
    consent_handoff.add_argument("task_id")
    for command, help_text in (
        ("orchestration-consent-verify",
         "verify external Ed25519 consent evidence without creating authorization"),
        ("orchestration-consent-reserve",
         "reserve consent evidence once for audit ONLY; never dispatch"),
    ):
        consent_command = sub.add_parser(command, help=help_text)
        consent_command.add_argument("task_id")
        consent_command.add_argument(
            "--receipt", type=_path_argument, required=True)
        consent_command.add_argument(
            "--trust-policy", type=_path_argument, required=True)
    escrow_reserve = sub.add_parser(
        "orchestration-predispatch-escrow-reserve",
        help="one-shot offline evidence escrow; NO platform POST or model call")
    escrow_reserve.add_argument("task_id")
    escrow_status = sub.add_parser(
        "orchestration-predispatch-escrow-status",
        help="read-only pre-POST escrow evidence status; NEVER dispatch BRAIN")
    escrow_status.add_argument("task_id")
    recovery_audit = sub.add_parser(
        "orchestration-simulation-recovery-audit",
        help="inspect persisted Simulation unknown outcomes; no BRAIN/model calls")
    recovery_audit.add_argument("task_id")
    release_gate = sub.add_parser(
        "orchestration-release-gate",
        help="integrated read-only production release checklist; NEVER authorize")
    release_gate.add_argument("task_id")
    release_gate.add_argument(
        "--platform-read-evidence-id",
        help="local BRAIN read-only observation ID; NEVER production authority")
    sim_dispatch_audit = sub.add_parser(
        "orchestration-simulation-dispatch-audit",
        help="read-only full Simulation write admission blockers; never dispatch")
    sim_dispatch_audit.add_argument("task_id")
    handoff = sub.add_parser(
        "orchestration-handoff",
        help="inspect prepared identity and existing domain model ledger without external calls")
    handoff.add_argument("task_id")
    recovery_check = sub.add_parser(
        "orchestration-recovery-check",
        help="read blocked task, Intent and model budget; never call BRAIN")
    recovery_check.add_argument("task_id")
    orchestration_status = sub.add_parser("orchestration-status", help="read a persisted orchestration task")
    orchestration_status.add_argument("task_id")
    run = sub.add_parser("run-once", help="execute one admitted research batch")
    run.add_argument("--objective", required=True)
    run.add_argument("--dataset")
    run.add_argument("--diagnosis-id")
    run.add_argument("--remedy-id")
    run.add_argument("--allow-remedy-round-overrun", action="store_true")
    series = sub.add_parser("run", help="execute a bounded serial research case")
    series.add_argument("--objective", required=True)
    series.add_argument("--dataset")
    series.add_argument("--max-batches", type=int, required=True)
    series.add_argument("--diagnosis-id")
    series.add_argument("--remedy-id")
    series.add_argument("--allow-remedy-round-overrun", action="store_true")
    planning = sub.add_parser("plan-regular", help="discover a Regular Campaign scope without simulation")
    planning.add_argument("--region")
    planning.add_argument("--dataset", action="append")
    planning.add_argument("--category")
    planning.add_argument("--delay", type=int)
    planning.add_argument("--universe")
    campaign = sub.add_parser("run-campaign", help="execute a bounded Regular Campaign")
    campaign_target = campaign.add_mutually_exclusive_group(required=True)
    campaign_target.add_argument("--objective")
    campaign_target.add_argument("--resume-campaign", metavar="ID", help="resume a saved Campaign")
    campaign.add_argument("--region")
    campaign.add_argument("--dataset", action="append")
    campaign.add_argument("--category")
    campaign.add_argument("--delay", type=int)
    campaign.add_argument("--universe")
    campaign.add_argument("--max-batches", type=int, help="optional legacy attempt limit per Dataset")
    campaign.add_argument("--ppa", action="store_true",
                          help="apply the optional PPA Profile")
    campaign.add_argument("--brain-credentials-stdin", action="store_true",
                          help=argparse.SUPPRESS)
    optimization = sub.add_parser(
        "plan-optimization", help="capture an existing Alpha as a read-only optimization baseline")
    optimization.add_argument("--alpha-id", required=True)
    optimization.add_argument("--objective")
    optimization.add_argument("--region")
    optimization_run = sub.add_parser(
        "run-optimization", help="simulate one independent Alpha optimization candidate")
    optimization_run.add_argument("--alpha-id", required=True)
    optimization_run.add_argument("--objective", required=True)
    optimization_run.add_argument("--region")
    super_plan = sub.add_parser(
        "plan-super", help="inspect BRAIN-native Super settings and active Alpha pool")
    super_plan.add_argument("--region")
    super_plan.add_argument("--objective", required=True)
    super_plan.add_argument("--delay", type=int)
    super_run = sub.add_parser(
        "run-super", help="execute one native Super research experiment")
    super_run.add_argument("--region")
    super_run.add_argument("--objective", required=True)
    super_run.add_argument("--delay", type=int)
    research_recovery = sub.add_parser("recover-research", help="reconcile a V1.1 Super or Optimization simulation intent without reposting")
    research_recovery.add_argument("intent_path", type=_path_argument)
    recovery = sub.add_parser("recover", help="reconcile an unfinished Regular batch")
    recovery.add_argument("batch_dir", type=_path_argument)
    unified_recovery = sub.add_parser(
        "recover-workflow",
        help="recover a Regular batch directory or a Super/Optimization simulation intent")
    unified_recovery.add_argument(
        "--workflow", choices=("regular", "super", "optimization"), required=True)
    unified_recovery.add_argument("target", type=_path_argument)
    task_recovery = sub.add_parser(
        "recover-task", help="reconcile a blocked orchestration execute step from its saved workflow intent")
    task_recovery.add_argument(
        "--workflow", choices=("regular", "super", "optimization"), required=True)
    task_recovery.add_argument("task_id")
    void = sub.add_parser("void", help="archive an unsubmitted draft")
    void.add_argument("batch_dir", type=_path_argument)
    void.add_argument("--reason", required=True)
    validity = sub.add_parser("evidence-validity",
                              help="mark an indexed evidence item with an explicit validity")
    validity.add_argument("evidence_id")
    validity.add_argument("--status", choices=("valid", "void", "falsified", "pending"),
                          required=True)
    validity.add_argument("--reason", required=True)
    validity.add_argument("--operator", default="user")
    knowledge = sub.add_parser("knowledge", help="inspect and maintain local research knowledge")
    knowledge_sub = knowledge.add_subparsers(dest="knowledge_command", required=True)
    listing = knowledge_sub.add_parser("list", help="list managed knowledge items")
    listing.add_argument("--kind", choices=("all", "operator", "field", "remedy", "evidence"),
                         default="all")
    show = knowledge_sub.add_parser("show", help="show one managed knowledge item")
    show.add_argument("--kind", choices=("operator", "field", "remedy", "evidence"),
                      required=True)
    show.add_argument("--id", required=True)
    note = knowledge_sub.add_parser("note", help="manage exact operator and field notes")
    note_sub = note.add_subparsers(dest="note_command", required=True)
    note_put = note_sub.add_parser("put", help="add or explicitly replace an exact note")
    note_put.add_argument("--kind", choices=("operator", "field"), required=True)
    note_put.add_argument("--key", required=True)
    note_put.add_argument("--content-file", type=_path_argument, required=True)
    note_put.add_argument("--source", required=True)
    note_put.add_argument("--date", default="unknown")
    note_put.add_argument("--validity", choices=("valid", "void", "falsified", "pending"),
                          default="valid")
    note_put.add_argument("--replace", action="store_true")
    note_put.add_argument("--reason")
    note_put.add_argument("--operator", default="user")
    remedy = knowledge_sub.add_parser("remedy", help="register and review remedies")
    remedy_sub = remedy.add_subparsers(dest="remedy_command", required=True)
    remedy_add = remedy_sub.add_parser("add", help="register a remedy as pending review")
    remedy_add.add_argument("--file", type=_path_argument, required=True)
    remedy_add.add_argument("--operator", default="user")
    remedy_review = remedy_sub.add_parser("review", help="review a registered remedy")
    remedy_review.add_argument("--id", required=True)
    remedy_review.add_argument("--status",
                               choices=("pending_review", "verified", "falsified", "void"),
                               required=True)
    remedy_review.add_argument("--reviewer", required=True)
    remedy_review.add_argument("--reason", required=True)
    search = knowledge_sub.add_parser("search", help="search local indexed evidence")
    search.add_argument("--dataset", default="*")
    search.add_argument("--mechanism", action="append", default=[])
    search.add_argument("--failure-signature", action="append", default=[])
    search.add_argument("--field-generation", action="append", default=[])
    search.add_argument("--region")
    search.add_argument("--delay", type=int)
    search.add_argument("--universe")
    search.add_argument("--text", default="")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--include-history", action="store_true",
                         help="return historical-only evidence; never mixed with valid results")
    sub.add_parser("status", help="inspect local state without platform access")
    sync_cache = sub.add_parser("sync-backtest-cache", help="index completed BRAIN IS and OS history")
    sync_cache.add_argument("--brain-credentials-stdin", action="store_true", help=argparse.SUPPRESS)
    tags = sub.add_parser("tag-alpha", help="preview or explicitly apply a verified local tag plan")
    tags.add_argument("alpha_id")
    tags.add_argument("--apply", action="store_true", help="apply the reviewed changes to BRAIN")
    tags.add_argument("--diagnostic", action="store_true", help="include audit diagnostic name and color")
    tags.add_argument("--brain-credentials-stdin", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("rebuild-pools", help="rebuild global Candidate/Preferred index without BRAIN requests")
    sub.add_parser("stop", help="block new simulations")
    sub.add_parser("resume", help="clear the stop flag by explicit command")
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
    if args.command in {"orchestration-prepare", "orchestration-handoff",
                        "orchestration-recovery-check", "orchestration-run",
                        "orchestration-domain-mock", "orchestration-admission-audit",
                        "orchestration-authorization-inspect",
                        "orchestration-authorization-reserve",
                        "orchestration-simulation-scope-check",
                        "orchestration-simulation-stage",
                        "orchestration-simulation-stage-status",
                        "orchestration-consent-status",
                        "orchestration-consent-verify",
                        "orchestration-consent-reserve",
                        "orchestration-scope-issuer-request",
                        "orchestration-manifest-status",
                        "orchestration-consent-issuer-request",
                        "orchestration-predispatch-escrow-reserve",
                        "orchestration-predispatch-escrow-status",
                        "orchestration-simulation-recovery-audit",
                        "orchestration-release-gate",
                        "orchestration-simulation-dispatch-audit"}:
        try:
            if set(registry_assignments) != {"research_lead", "research_executor"}:
                raise OrchestrationError(
                    "both --lead and --executor registrations are required")
            config_path = args.config or root / "config" / "nastu_quant.json"
            cfg, config_source = load_preparation_config(
                config_path, explicit=args.config is not None)
            prepared = prepare_registered_model_runtime(
                cfg, registry_assignments, registry_path)
            policy = prepared["model_governance"]
            effective = prepared["model"]
            if args.command == "orchestration-simulation-dispatch-audit":
                result = inspect_simulation_dispatch_gate(
                    MainOrchestrator(root), args.task_id, policy)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.command == "orchestration-release-gate":
                result = inspect_production_release_gate(
                    MainOrchestrator(root), args.task_id,
                    effective, policy, config_source=config_source,
                    platform_read_evidence_id=args.platform_read_evidence_id)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.command == "orchestration-simulation-recovery-audit":
                result = inspect_simulation_recovery_evidence(
                    MainOrchestrator(root), args.task_id, policy)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.command == "orchestration-predispatch-escrow-reserve":
                result = reserve_pre_dispatch_escrow(
                    MainOrchestrator(root), args.task_id, policy)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.command == "orchestration-manifest-status":
                result = inspect_generated_manifest(
                    MainOrchestrator(root), args.task_id)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.command == "orchestration-scope-issuer-request":
                if args.manifest is not None and not args.manifest.is_file():
                    print(
                        "orchestration preparation failed: --manifest file not found; "
                        "provide the real path to a separately reviewed "
                        "Simulation manifest JSON (example paths are not files)",
                        file=sys.stderr)
                    return 2
                manifest = _resolve_simulation_manifest(
                    MainOrchestrator(root), args.task_id, args.manifest)
                if manifest is None:
                    result = inspect_generated_manifest(
                        MainOrchestrator(root), args.task_id)
                    print(json.dumps(result, ensure_ascii=False, indent=2))
                    return 0
                result = build_scope_issuer_request(
                    MainOrchestrator(root), args.task_id,
                    effective, policy, manifest)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.command == "orchestration-consent-issuer-request":
                result = build_operator_consent_issuer_request(
                    MainOrchestrator(root), args.task_id, effective, policy)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.command in {"orchestration-consent-verify",
                                "orchestration-consent-reserve"}:
                receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
                trust_policy = json.loads(
                    args.trust_policy.read_text(encoding="utf-8"))
                if args.command == "orchestration-consent-verify":
                    result = verify_external_consent(
                        MainOrchestrator(root), args.task_id,
                        effective, policy, receipt, trust_policy)
                else:
                    result = reserve_consent_for_audit(
                        MainOrchestrator(root), args.task_id,
                        effective, policy, receipt, trust_policy)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.command == "orchestration-predispatch-escrow-status":
                result = inspect_pre_dispatch_escrow(
                    MainOrchestrator(root), args.task_id, policy)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.command == "orchestration-consent-status":
                result = inspect_consent_reservation(
                    MainOrchestrator(root), args.task_id)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.command == "orchestration-simulation-stage-status":
                result = inspect_staged_simulation_attempt(
                    MainOrchestrator(root), args.task_id)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.command == "orchestration-simulation-stage":
                receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
                manifest = _resolve_simulation_manifest(
                    MainOrchestrator(root), args.task_id, args.manifest)
                if manifest is None:
                    raise OrchestrationError(
                        "Agent Simulation manifest not ready; no research candidate recorded")
                result = stage_simulation_attempt(
                    MainOrchestrator(root), args.task_id, effective, policy,
                    receipt, manifest)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.command == "orchestration-simulation-scope-check":
                # Read-only verifier: no lock, Gateway, model or intent write.
                receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
                manifest = _resolve_simulation_manifest(
                    MainOrchestrator(root), args.task_id, args.manifest)
                if manifest is None:
                    raise OrchestrationError(
                        "Agent Simulation manifest not ready; no research candidate recorded")
                result = inspect_simulation_scope(
                    MainOrchestrator(root), args.task_id, effective, policy,
                    receipt, manifest)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.command == "orchestration-authorization-reserve":
                # The only write is a single-use non-executable audit journal.
                # There is no provider, Gateway, Simulation or dispatch here.
                receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
                result = reserve_non_executable_receipt(
                    MainOrchestrator(root), args.task_id, effective, policy,
                    receipt)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.command == "orchestration-authorization-inspect":
                # Verify-only receipt path. No writer lock, no admission journal
                # reservation, no provider or gateway constructed.
                receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
                result = inspect_execution_receipt(
                    MainOrchestrator(root), args.task_id, effective, policy,
                    receipt)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            if args.command == "orchestration-admission-audit":
                # A read-only audit must not create a writer/execution lock file.
                result = audit_execution_admission(
                    MainOrchestrator(root), args.task_id, effective, policy,
                    config_source=config_source)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            with SingleWriterLock(root / "state" / "agent.lock"):
                runner = MainOrchestrator(root)
                if args.command == "orchestration-prepare":
                    request = {"objective": args.objective}
                    if args.workflow == "regular":
                        if args.alpha_id:
                            raise OrchestrationError("Alpha ID applies only to optimization")
                        scope = _regular_scope(args, cfg)
                        if not scope["region"]:
                            raise OrchestrationError("Region is required")
                        if scope["delay"] not in (None, 1):
                            raise OrchestrationError("Regular Campaign requires Delay 1")
                        request.update(scope)
                        request["delay"] = 1
                        request["ppa_profile"] = args.ppa
                    else:
                        if args.dataset or args.category or args.ppa:
                            raise OrchestrationError(
                                "Dataset, Category and PPA apply only to Regular")
                        fixed_scope = cfg.get("scope") or {}
                        request["region"] = _constraint(
                            args.region, fixed_scope.get("region"), "Region")
                        request["delay"] = _constraint(
                            args.delay, fixed_scope.get("delay"), "Delay")
                        # A Simulation receipt cannot expand an unset scope.
                        # Pin the Universe here, not during later staging.
                        request["universe"] = _constraint(
                            args.universe, fixed_scope.get("universe"), "Universe")
                        if not request["region"]:
                            raise OrchestrationError("Region is required")
                        if args.workflow == "optimization":
                            if not args.alpha_id:
                                raise OrchestrationError("optimization requires Alpha ID")
                            request["alpha_id"] = args.alpha_id
                        elif args.alpha_id:
                            raise OrchestrationError("Alpha ID applies only to optimization")
                    task = runner.create(args.workflow, request, budget=1)
                    proposal = persist_execution_proposal(runner, task["task_id"])
                    identity = persist_execution_identity(
                        runner, task["task_id"], effective, policy)
                    result = {
                        "status": "PREPARED_NOT_AUTHORIZED",
                        "config_source": config_source,
                        "task_id": task["task_id"],
                        "proposal_sha256": proposal["proposal_sha256"],
                        "identity_sha256": identity["identity_sha256"],
                        "model_call_authorized": False,
                        "brain_invoked": False,
                    }
                elif args.command == "orchestration-run":
                    from alpha_research.orchestration.execution_identity import inspect_execution_identity
                    from alpha_research.orchestration.execution_proposal import inspect_execution_proposal
                    task = runner.load(args.task_id)
                    if task["status"] != "CREATED":
                        raise OrchestrationError(
                            "synthetic run requires a CREATED task; never replay existing execution")
                    inspect_execution_proposal(runner, args.task_id)
                    inspect_execution_identity(
                        runner, args.task_id, effective, policy)
                    adapter = build_prepared_mock_adapter(
                        task["workflow"], task["request"])
                    runner.adapter = adapter
                    result = runner.execute(args.task_id)
                    result = {
                        "status": result["status"], "task_id": result["task_id"],
                        "mode": "synthetic_fixture",
                        "domain_dispatch_invoked": False,
                        "model_invoked": False, "brain_invoked": False,
                        "task": result,
                    }
                elif args.command == "orchestration-domain-mock":
                    from alpha_research.orchestration.execution_identity import inspect_execution_identity
                    from alpha_research.orchestration.execution_proposal import inspect_execution_proposal
                    task = runner.load(args.task_id)
                    if task["status"] != "CREATED":
                        raise OrchestrationError(
                            "domain mock requires a fresh CREATED task; no replay")
                    inspect_execution_proposal(runner, args.task_id)
                    inspect_execution_identity(
                        runner, args.task_id, effective, policy)
                    # A domain mock may NEVER take over the default research root.
                    # Require a separately supplied --state-root for isolation.
                    if root == default_state_root().resolve():
                        raise OrchestrationError(
                            "domain mock requires a dedicated non-default --state-root")
                    # Validate the workspace before any domain evidence can be written.
                    # The marker is permanent and never grants production authority.
                    inspect_mock_workspace(root)
                    adapter, mock_gateway, mock_agent = build_domain_mock_adapter(
                        root, task, fault=args.fault)
                    claim_mock_workspace(root)
                    runner.adapter = adapter
                    with mock_domain_boundary(task["request"]["region"]):
                        completed = runner.execute(args.task_id)
                    result = {
                        "task_id": completed["task_id"],
                        "status": completed["status"],
                        "mode": "offline_domain_mock",
                        "workflow": completed["workflow"],
                        "gateway_calls": mock_gateway.calls,
                        "mock_simulation_attempts": (
                            mock_gateway.calls.count("simulate_super")
                            if task["workflow"] != "regular" else mock_agent.calls),
                        "model_provider_invoked": False,
                        "brain_provider_invoked": False,
                        "execution": completed,
                    }
                elif args.command == "orchestration-handoff":
                    result = inspect_domain_research_handoff(
                        runner, args.task_id, effective, policy)
                else:
                    result = inspect_task_recovery_preflight(
                        runner, args.task_id, effective, policy)
        except (OSError, ValueError, TypeError, KeyError, WriterLockError) as exc:
            # Show safe configuration-path diagnostics without echoing credentials,
            # model backend payloads or provider errors.
            if isinstance(exc, FileNotFoundError):
                print(f"orchestration preparation failed: {exc}", file=sys.stderr)
            elif isinstance(exc, ValueError) and str(exc).startswith(
                    ("research configuration is ", "research configuration must ",
                     "research configuration file ", "research configuration is missing",
                     "requested Region conflicts with configured scope",
                     "requested Delay conflicts with configured scope",
                     "requested Universe conflicts with configured scope",
                     "Super/Optimization Simulation requires explicit Universe",
                     "Dataset, Category and PPA apply only to Regular",
                     "Agent Simulation manifest not ready;",
                     "external manifest conflicts with Agent-pinned")):
                print(f"orchestration preparation failed: {exc}", file=sys.stderr)
            else:
                print(f"orchestration preparation failed: {type(exc).__name__}",
                      file=sys.stderr)
            return 2
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
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
        config_path = args.config or root / "config" / "nastu_quant.json"
        config_status = "loaded"
        try:
            pool_config = json.loads(config_path.read_text(encoding="utf-8"))
            if not isinstance(pool_config, dict):
                raise ValueError("configuration must be an object")
            current_policy = policy_hash(pool_config)
        except FileNotFoundError:
            config_status = "missing"
            current_policy = policy_hash(DEFAULT_CONFIG)
        except (OSError, ValueError, TypeError):
            config_status = "unreadable"
            current_policy = None
        runs = root / "runs"
        unfinished = []
        if runs.exists():
            for context in runs.rglob("batch_context.json"):
                batch_dir = context.parent
                if (batch_dir / "void.json").exists():
                    continue
                intent_path = batch_dir / "simulation_intent.json"
                results_path = batch_dir / "results.json"
                try:
                    closed = (results_path.exists() and intent_path.exists() and
                              json.loads(intent_path.read_text(encoding="utf-8")).get(
                                  "status") == "READ_BACK")
                except (OSError, ValueError):
                    closed = False
                if not closed:
                    unfinished.append(str(batch_dir))
                    continue
                prereg_path = batch_dir / "prereg.json"
                if prereg_path.exists():
                    try:
                        prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
                        if prereg.get("remedy_reference"):
                            diagnosis = json.loads((batch_dir / "diagnosis.json").read_text(
                                encoding="utf-8"))
                            if (diagnosis.get("remedy_outcome") or {}).get("status") != "recorded":
                                unfinished.append(str(batch_dir))
                    except (OSError, ValueError):
                        unfinished.append(str(batch_dir))
            for batch_dir in runs.glob("*/*/*/*/*"):
                if (batch_dir.is_dir() and
                        re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{8}", batch_dir.name) and
                        not (batch_dir / "batch_context.json").exists() and
                        not (batch_dir / "void.json").exists() and
                        str(batch_dir) not in unfinished):
                    unfinished.append(str(batch_dir))
        campaign_root = root / "state" / "campaigns"
        campaigns = []
        if campaign_root.exists():
            for path in sorted(campaign_root.glob("*.json")):
                try:
                    campaigns.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    campaigns.append({"path": str(path), "status": "unreadable"})
        v11_campaigns = []
        v11_root = root / "state" / "research_v1_1" / "campaign_runs"
        if v11_root.exists():
            for path in sorted(v11_root.glob("*.json")):
                if not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
                    continue
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                    if saved.get("deleted_at"):
                        continue
                    v11_campaigns.append({
                        "campaign_id": saved.get("campaign_id"),
                        "status": saved.get("status"),
                        "batches_completed": len(saved.get("completed", [])),
                        "batches_attempted": saved.get("budget", {}).get("used", {}).get("batches"),
                        "subtasks": saved.get("subtasks", []),
                        "active_item": saved.get("active_item"),
                        "resumable": campaign_can_resume(saved),
                        "has_summary": path.with_name(f"{path.stem}_summary.json").is_file(),
                        "path": str(path)})
                except (OSError, ValueError):
                    v11_campaigns.append({"path": str(path), "status": "unreadable"})
        def _record_summaries(kind):
            storage_kind = {"candidate_evaluations": "candidate",
                            "preferred_evaluations": "preferred"}.get(kind, kind)
            folder = root / "state" / "research_v1_1" / storage_kind
            output = []
            if folder.exists():
                for record_path in sorted(folder.glob("*.json")):
                    try:
                        item = json.loads(record_path.read_text(encoding="utf-8"))
                        if storage_kind == "candidate" and item.get("status") != "CANDIDATE":
                            continue
                        if storage_kind == "preferred" and item.get("status") != "PREFERRED":
                            continue
                        policy_current = (current_policy is not None and
                                          item.get("policy_sha256") == current_policy)
                        candidate_status = item.get("candidate_status") or (
                            item.get("status") if storage_kind == "candidate" else None)
                        preferred_status = item.get("preferred_status") or (
                            item.get("status") if storage_kind == "preferred" else None)
                        if storage_kind == "candidate" and item.get("status") == "CANDIDATE" and not policy_current:
                            candidate_status = "POLICY_STALE"
                            preferred_status = "NOT_CANDIDATE"
                        if storage_kind == "preferred" and item.get("status") == "PREFERRED" and not policy_current:
                            preferred_status = "NOT_CANDIDATE"
                        output.append({
                            "id": item.get("candidate_id") or item.get("run_id") or item.get("branch_id") or
                                  item.get("source_id"),
                            "source_alpha_id": item.get("source_alpha_id") or item.get("alpha_id"),
                            "workflow": item.get("workflow"),
                            "candidate_status": candidate_status,
                            "preferred_status": preferred_status,
                            "policy_current": policy_current,
                            "candidate_alpha_id": item.get("candidate_alpha_id"),
                            "status": item.get("status"),
                            "simulation_intent_ref": item.get("simulation_intent_ref"),
                            "response_ref": item.get("response_ref"),
                            "receipt_ref": item.get("receipt_ref") or item.get("readback_ref"),
                            "audit_ref": item.get("audit_ref"),
                            "report_ref": item.get("report_ref"),
                            "path": str(record_path)})
                    except (OSError, ValueError):
                        output.append({"path": str(record_path), "status": "unreadable"})
            return output
        try:
            if current_policy is None:
                raise ValueError("current admission policy is unavailable")
            global_pool = CandidatePool(ResearchRecordStore(root)).build_index(
                current_policy_sha256=current_policy)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            global_pool = {"status": "unavailable", "reason": type(exc).__name__}
        print(json.dumps({"global_alpha_pool": global_pool,
                          "research_config": {"status": config_status, "path": str(config_path)},
                          "catalog_snapshot_root": str(CATALOG_SNAPSHOT_ROOT),
                          "stopped": stop_flag.exists(),
                          "unfinished_batches": unfinished,
                          "campaigns": campaigns,
                          "v11_campaigns": v11_campaigns,
                          "v11_super_runs": _record_summaries("super_run"),
                          "v11_optimization_sources": _record_summaries("optimization_source"),
                          "v11_candidate_branches": _record_summaries("candidate_branch"),
                          "v11_candidates": [
                              row for row in global_pool.get("entries", [])
                              if row["candidate_status"] == "CANDIDATE"],
                          "v11_preferred": [
                              row for row in global_pool.get("entries", [])
                              if row["candidate_status"] == "CANDIDATE" and
                                 row["preferred_status"] == "PREFERRED"],
                          "v11_candidate_evaluations": _record_summaries("candidate_evaluations"),
                          "v11_preferred_evaluations": _record_summaries("preferred_evaluations")},
                         ensure_ascii=False))
        return 0
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
