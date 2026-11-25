"""Orchestration command adapter.

Owns the prepare/audit/mock command dispatch and single CLI-scoped
orchestrator. All production checks remain negative-only; no Gateway or paid
model is created here. Independently mockable consent operations are injected
by the entrypoint to preserve command-level test and security boundaries.
"""
import json
import sys

from alpha_research.state.paths import default_state_root
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError
from alpha_research.orchestration.dispatch import _regular_scope, _constraint
from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.admission_audit import audit_execution_admission
from alpha_research.orchestration.execution_authorization import inspect_execution_receipt
from alpha_research.orchestration.execution_reservation import reserve_non_executable_receipt
from alpha_research.orchestration.simulation_scope import inspect_simulation_scope
from alpha_research.orchestration.simulation_attempt_admission import (
    stage_simulation_attempt, inspect_staged_simulation_attempt,
)
from alpha_research.orchestration.simulation_dispatch_preflight import inspect_simulation_dispatch_gate
from alpha_research.orchestration.operator_consent import inspect_consent_reservation
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
from alpha_research.orchestration.research_catalog_protocol import inspect_catalog_read_contract
from alpha_research.orchestration.research_runtime_adapter import inspect_runtime_attempt
from alpha_research.orchestration.research_runtime_plan import inspect_research_runtime_plan
from alpha_research.orchestration.research_control_plane import inspect_research_control_plane
from alpha_research.orchestration.pre_dispatch_escrow import (
    inspect_pre_dispatch_escrow, reserve_pre_dispatch_escrow,
)
from alpha_research.orchestration.simulation_recovery_audit import inspect_simulation_recovery_evidence
from alpha_research.orchestration.release_gate import inspect_production_release_gate
from alpha_research.orchestration.runtime_config import (
    prepare_registered_model_runtime, load_preparation_config,
)
from alpha_research.config import is_default_config_path
from alpha_research.orchestration.recovery_preflight import inspect_task_recovery_preflight
from alpha_research.orchestration.execution_proposal import persist_execution_proposal
from alpha_research.orchestration.execution_identity import (
    persist_execution_identity, inspect_domain_research_handoff,
)
from alpha_research.orchestration.prepared_mock import build_prepared_mock_adapter
from alpha_research.orchestration.domain_mock import build_domain_mock_adapter, mock_domain_boundary
from alpha_research.orchestration.mock_workspace import claim_mock_workspace, inspect_mock_workspace
from alpha_research.orchestration.execution_trace import inspect_execution_trace


ORCHESTRATION_COMMANDS = frozenset({"orchestration-execution-trace",
                        "orchestration-local-pilot-readiness",
                        "orchestration-prepare", "orchestration-handoff",
                        "orchestration-recovery-check", "orchestration-run",
                        "orchestration-domain-mock",
                        "orchestration-research-prepare",
                        "orchestration-research-prepare-status",
                        "orchestration-research-runtime-status",
                        "orchestration-research-runtime-plan",
                        "orchestration-research-status",
                        "orchestration-research-issuer-request",
                        "orchestration-research-live-readiness",
                        "orchestration-research-grant-audit-reserve",
                        "orchestration-research-grant-audit-status",
                        "orchestration-research-catalog-contract",
                        "orchestration-admission-audit",
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
                        "orchestration-simulation-dispatch-audit"})


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



def run_orchestration_command(args, root, registry_assignments, registry_path, *,
                              verify_external_consent, reserve_consent_for_audit):
    try:
        if args.command == "orchestration-execution-trace":
            result = inspect_execution_trace(root, args.task_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if set(registry_assignments) != {"research_lead", "research_executor"}:
            raise OrchestrationError(
                "both --lead and --executor registrations are required")
        config_path = args.config or root / "config" / "nastu_quant.json"
        cfg, config_source = load_preparation_config(
            config_path,
            explicit=(args.config is not None
                      and not is_default_config_path(config_path, root)))
        prepared = prepare_registered_model_runtime(
            cfg, registry_assignments, registry_path)
        policy = prepared["model_governance"]
        effective = prepared["model"]
        orchestrator = MainOrchestrator(root)
        if args.command == "orchestration-local-pilot-readiness":
            # Reuse the exact Pilot task/identity/campaign guards, but never
            # construct Gateway/Model or reserve any simulation/model budget.
            from alpha_research.orchestration.local_regular_pilot import (
                validate_local_pilot_task,
            )
            from alpha_research.preflight import local_preflight
            blockers = []
            scope = None
            try:
                request = validate_local_pilot_task(
                    orchestrator, args.task_id, effective, policy)
                scope = {
                    key: request.get(key) for key in (
                        "region", "delay", "universe", "dataset_ids", "category")
                }
                # A ready preflight Region alone does not prove the persisted
                # Scope still matches the immutable Task. Check all pinned
                # dimensions before suggesting any future real Pilot.
                configured = prepared.get("scope") or {}
                for field in ("region", "delay", "universe", "category"):
                    fixed = configured.get(field)
                    if fixed is not None and (
                            type(fixed) is not type(request.get(field))
                            or fixed != request.get(field)):
                        blockers.append("configured " + field + " conflicts with Task")
                dataset = configured.get("dataset_id")
                if dataset is not None and request.get("dataset_ids") != [dataset]:
                    blockers.append("configured dataset_id conflicts with Task")
            except (OSError, ValueError, TypeError) as exc:
                blockers.append(str(exc))
            local = local_preflight(
                config_path, registry_path, lead=args.lead,
                executor=args.executor,
                region=scope["region"] if scope is not None else None)
            for field in (
                    "config", "scope", "models", "model_budget",
                    "brain_credentials"):
                if local[field] != "READY_LOCAL":
                    blockers.append("local " + field + ": " + local[field])
            report = {
                "schema_version": 1,
                "status": (
                    "PILOT_LOCAL_READY_NOT_AUTHORIZED" if not blockers
                    else "PILOT_LOCAL_BLOCKED"),
                "task_id": args.task_id,
                "workflow": "regular",
                "scope": scope,
                "config_source": config_source,
                "blockers": blockers,
                "local_checks": {key: local[key] for key in (
                    "config", "scope", "models", "model_budget",
                    "brain_credentials")},
                "model_invoked": False,
                "brain_invoked": False,
                "brain_read_verified": False,
                "simulation_authorized": False,
                "alpha_submission_authorized": False,
                "live_dispatch_invoked": False,
                "explicit_user_authorization_required": True,
            }
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if not blockers else 2
        if args.command == "orchestration-research-prepare-status":
            result = inspect_research_preparation(
                orchestrator, args.task_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-research-runtime-status":
            result = inspect_runtime_attempt(
                orchestrator, args.task_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-research-status":
            result = inspect_research_control_plane(
                orchestrator, args.task_id, effective, policy)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-research-runtime-plan":
            if (args.receipt is None) != (args.trust_policy is None):
                raise OrchestrationError(
                    "research receipt and trust must be supplied together")
            if (args.global_consumption_ack is None) != (
                    args.consumer_trust_policy is None):
                raise OrchestrationError(
                    "global consumption ACK and consumer trust must be paired")
            receipt = (json.loads(args.receipt.read_text(encoding="utf-8"))
                       if args.receipt is not None else None)
            trust = (json.loads(args.trust_policy.read_text(encoding="utf-8"))
                     if args.trust_policy is not None else None)
            global_ack = (
                json.loads(args.global_consumption_ack.read_text(encoding="utf-8"))
                if args.global_consumption_ack is not None else None)
            consumer_trust = (
                json.loads(args.consumer_trust_policy.read_text(encoding="utf-8"))
                if args.consumer_trust_policy is not None else None)
            result = inspect_research_runtime_plan(
                orchestrator, args.task_id, effective, policy,
                receipt=receipt, trust_policy=trust,
                input_tokens=args.input_tokens,
                max_output_tokens=args.max_output_tokens,
                global_ack=global_ack, consumer_trust=consumer_trust)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-research-grant-audit-status":
            result = inspect_research_grant_claim(
                orchestrator, args.task_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-research-catalog-contract":
            result = inspect_catalog_read_contract(
                orchestrator, args.task_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-research-grant-audit-reserve":
            receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
            trust = json.loads(args.trust_policy.read_text(encoding="utf-8"))
            result = reserve_research_grant_audit(
                orchestrator, args.task_id,
                effective, policy, receipt, trust)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-research-issuer-request":
            result = build_research_issuer_request(
                orchestrator, args.task_id, effective, policy)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-research-live-readiness":
            if (args.receipt is None) != (args.trust_policy is None):
                raise OrchestrationError(
                    "research receipt and issuer trust policy must be paired")
            receipt = (json.loads(args.receipt.read_text(encoding="utf-8"))
                       if args.receipt is not None else None)
            trust = (json.loads(args.trust_policy.read_text(encoding="utf-8"))
                     if args.trust_policy is not None else None)
            result = inspect_research_live_readiness(
                orchestrator, args.task_id, effective, policy,
                config_source=config_source,
                receipt=receipt, trust_policy=trust)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-simulation-dispatch-audit":
            result = inspect_simulation_dispatch_gate(
                orchestrator, args.task_id, policy)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-release-gate":
            result = inspect_production_release_gate(
                orchestrator, args.task_id,
                effective, policy, config_source=config_source,
                platform_read_evidence_id=args.platform_read_evidence_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-simulation-recovery-audit":
            result = inspect_simulation_recovery_evidence(
                orchestrator, args.task_id, policy)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-predispatch-escrow-reserve":
            result = reserve_pre_dispatch_escrow(
                orchestrator, args.task_id, policy)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-manifest-status":
            result = inspect_generated_manifest(
                orchestrator, args.task_id)
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
                orchestrator, args.task_id, args.manifest)
            if manifest is None:
                result = inspect_generated_manifest(
                    orchestrator, args.task_id)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            result = build_scope_issuer_request(
                orchestrator, args.task_id,
                effective, policy, manifest)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-consent-issuer-request":
            result = build_operator_consent_issuer_request(
                orchestrator, args.task_id, effective, policy)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command in {"orchestration-consent-verify",
                            "orchestration-consent-reserve"}:
            receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
            trust_policy = json.loads(
                args.trust_policy.read_text(encoding="utf-8"))
            if args.command == "orchestration-consent-verify":
                result = verify_external_consent(
                    orchestrator, args.task_id,
                    effective, policy, receipt, trust_policy)
            else:
                result = reserve_consent_for_audit(
                    orchestrator, args.task_id,
                    effective, policy, receipt, trust_policy)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-predispatch-escrow-status":
            result = inspect_pre_dispatch_escrow(
                orchestrator, args.task_id, policy)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-consent-status":
            result = inspect_consent_reservation(
                orchestrator, args.task_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-simulation-stage-status":
            result = inspect_staged_simulation_attempt(
                orchestrator, args.task_id)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-simulation-stage":
            receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
            manifest = _resolve_simulation_manifest(
                orchestrator, args.task_id, args.manifest)
            if manifest is None:
                raise OrchestrationError(
                    "Agent Simulation manifest not ready; no research candidate recorded")
            result = stage_simulation_attempt(
                orchestrator, args.task_id, effective, policy,
                receipt, manifest)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-simulation-scope-check":
            # Read-only verifier: no lock, Gateway, model or intent write.
            receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
            manifest = _resolve_simulation_manifest(
                orchestrator, args.task_id, args.manifest)
            if manifest is None:
                raise OrchestrationError(
                    "Agent Simulation manifest not ready; no research candidate recorded")
            result = inspect_simulation_scope(
                orchestrator, args.task_id, effective, policy,
                receipt, manifest)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-authorization-reserve":
            # The only write is a single-use non-executable audit journal.
            # There is no provider, Gateway, Simulation or dispatch here.
            receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
            result = reserve_non_executable_receipt(
                orchestrator, args.task_id, effective, policy,
                receipt)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-authorization-inspect":
            # Verify-only receipt path. No writer lock, no admission journal
            # reservation, no provider or gateway constructed.
            receipt = json.loads(args.receipt.read_text(encoding="utf-8"))
            result = inspect_execution_receipt(
                orchestrator, args.task_id, effective, policy,
                receipt)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.command == "orchestration-admission-audit":
            # A read-only audit must not create a writer/execution lock file.
            result = audit_execution_admission(
                orchestrator, args.task_id, effective, policy,
                config_source=config_source)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        with SingleWriterLock(root / "state" / "agent.lock"):
            runner = orchestrator
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
            elif args.command == "orchestration-research-prepare":
                from alpha_research.orchestration.execution_identity import (
                    inspect_execution_identity,
                )
                from alpha_research.orchestration.execution_proposal import (
                    inspect_execution_proposal,
                )
                if root == default_state_root().resolve():
                    raise OrchestrationError(
                        "research preparation mock requires a dedicated non-default --state-root")
                task = runner.load(args.task_id)
                if task["status"] != "CREATED":
                    raise OrchestrationError(
                        "research preparation requires a CREATED Task; no replay")
                inspect_execution_proposal(runner, args.task_id)
                inspect_execution_identity(
                    runner, args.task_id, effective, policy)
                stop = root / "state" / "stop.flag"
                if stop.exists() or stop.is_symlink():
                    raise OrchestrationError(
                        "research preparation blocked by stop signal")
                inspect_mock_workspace(root)
                claim_mock_workspace(root)
                result = prepare_offline_research(runner, args.task_id)
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
    except (OSError, ValueError, TypeError, KeyError, RuntimeError,
            WriterLockError) as exc:
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
                 "external manifest conflicts with Agent-pinned",
                 "research receipt and issuer trust policy must be paired",
                 "research receipt and trust must be supplied together",
                 "model request limits require a signed research receipt",
                 "global consumption ACK and consumer trust must be paired",
                 "global consumption ACK requires signed research grant")):
            print(f"orchestration preparation failed: {exc}", file=sys.stderr)
        else:
            print(f"orchestration preparation failed: {type(exc).__name__}",
                  file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
