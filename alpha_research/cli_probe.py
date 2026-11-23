"""Explicit BRAIN read-only connectivity and local preflight CLI adapter.

This module never invokes a paid model, Simulation POST or Alpha submission.
BRAIN reads require the caller's explicit --allow-brain-read opt-in.
"""
import json
import os
import sys

from alpha_research.state.paths import expand_path
from alpha_research.orchestration.mock_workspace import marker_path
from alpha_research.orchestration.platform_read_evidence import (
    record_platform_read_evidence, inspect_platform_read_evidence,
)
from alpha_research.orchestration.readonly_probe import run_readonly_probe
from alpha_research.preflight import local_preflight

PROBE_COMMANDS = frozenset((
    "brain-read-evidence-status", "brain-readonly-check", "preflight",
))


def run_probe_command(args, root, registry_path, *, gateway_factory):
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
    gateway = gateway_factory(read_only=True)
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
