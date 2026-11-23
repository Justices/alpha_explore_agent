"""Model registry, read-only doctor, bounded smoke and budget CLI adapter.

Live paid probe requires --allow-model-call and does not grant BRAIN access.
An unknown outcome does not permit fallback, refund or implicit retry.
"""
import json
import sys

from alpha_research.agents.registry import (
    ModelRegistryError, load_registry, describe_registry, select_registered_backends,
    load_role_profile, assert_live_model_routes,
)
from alpha_research.agents.inspect import inspect_roles
from alpha_research.agents.doctor import check_registered_roles
from alpha_research.agents.probe import run_model_probe, ModelProbeError
from alpha_research.agents.governance import ModelCallLedger, ModelBudgetError
from alpha_research.integrations.model import ModelError


def run_models_command(args, root, registry_path, registry_assignments):
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
