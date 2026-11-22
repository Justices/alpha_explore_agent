"""Line-delimited JSON bridge to this project's independent BRAIN runtime."""

import json
import sys
from pathlib import Path

IMPORT_ROOT = Path(__file__).resolve().parents[3]
if str(IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPORT_ROOT))

from alpha_research.integrations.brain.runtime import PlatformRuntime, WorldQuantClient  # noqa: E402
from alpha_research.integrations.brain.guarded_protocol import (
    GUARDED_ORCHESTRATION_READS, GUARDED_FORBIDDEN_CLAIMS,
    guarded_request_allowed,
)  # noqa: E402
from alpha_research.orchestration.research_catalog_protocol import (
    research_catalog_request_allowed,
)  # noqa: E402


def research_catalog_frame_allowed(request, scope, *, source_alpha_id=None):
    """Driver-side exact research read frame guard; never issues authority."""
    if not isinstance(request, dict) or "cmd" not in request:
        return False
    return research_catalog_request_allowed(
        request["cmd"],
        {k: v for k, v in request.items() if k != "cmd"},
        scope, source_alpha_id=source_alpha_id,
    )


def inspect_research_catalog_evidence_frame(
        orchestrator, task_id, model_config, governance_policy,
        receipt, operator_trust, global_ack, consumer_trust,
        ledger_readback, ledger_trust, driver_evidence,
        driver_trust, frame, *, now=None):
    """P5.2 driver-side signed evidence and read-frame audit, never dispatch.

    Keep this check in the driver module so an external Gateway caller cannot
    substitute an unchecked boolean. Real research startup still
    refuses before credentials; future transport must recheck here.
    """
    from alpha_research.orchestration.research_driver_read_gate import (
        inspect_evidence_bound_driver_frame,
    )
    return inspect_evidence_bound_driver_frame(
        orchestrator, task_id, model_config, governance_policy,
        receipt, operator_trust, global_ack, consumer_trust,
        ledger_readback, ledger_trust, driver_evidence,
        driver_trust, frame, now=now)


def write(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def main():
    args = sys.argv[1:]
    read_only = "--read-only" in args
    guarded = "--orchestration-guarded" in args
    research_catalog = "--research-catalog" in args
    flags = {"--read-only", "--orchestration-guarded", "--research-catalog"}
    remaining = [arg for arg in args if arg not in flags]
    if (len(remaining) != len(args) - int(read_only) - int(guarded)
            - int(research_catalog)
            or remaining not in ([], ["--brain-credentials-stdin"])
            or (research_catalog and (read_only or guarded))):
        write({"ok": False, "stage": "configuration",
               "error": "invalid driver options"})
        return 2
    if research_catalog:
        # P4.19: this profile is not executable until an independently
        # trusted global grant consumer is provisioned. Refuse BEFORE
        # credentials are read or a platform client is constructed.
        # The pure driver-frame policy above is independently testable
        # with fake data. A signed file/CLI flag must never unlock it.
        write({"ok": False, "stage": "admission",
               "error": "independent research grant consumer unavailable"})
        return 2
    try:
        if remaining == ["--brain-credentials-stdin"]:
            credentials = json.loads(sys.stdin.readline())
            runtime = PlatformRuntime(client=WorldQuantClient(credentials=credentials))
        else:
            runtime = PlatformRuntime()
    except Exception as exc:
        # Do not reflect credential/account/provider details in guarded
        # orchestration session responses. The Gateway also redacts errors.
        error = ("guarded platform configuration failed" if guarded
                 else str(exc)[:1000])
        write({"ok": False, "stage": "configuration", "error": error})
        return 2

    write({"ok": True, "stage": "ready"})
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
                command = request.get("cmd")
                if command == "shutdown":
                    write({"ok": True, "stage": "shutdown"})
                    break
                # Even raw stdin access must not bypass the orchestration
                # production admission boundary. This option is NOT a live
                # enablement; unimplemented trusted grant consumption means
                # every mutating research command is refused by construction.
                if guarded and command not in GUARDED_ORCHESTRATION_READS:
                    write({"ok": False, "error":
                           "orchestration live admission is not implemented"})
                    continue
                if guarded and not guarded_request_allowed(
                        command, {key: value for key, value in request.items()
                                  if key != "cmd"}):
                    write({"ok": False, "error":
                           "untrusted orchestration authorization claims refused"})
                    continue
                # Independent driver-side authorization barrier: a caller
                # cannot bypass Gateway.read_only by writing raw JSON to stdin.
                if read_only and command not in {
                    "get_platform_setting_options", "readonly_probe_datasets",
                }:
                    write({"ok": False, "error":
                           "read-only driver refuses this command"})
                    continue
                params = {key: value for key, value in request.items() if key != "cmd"}
                result = runtime.dispatch(command, params)
                write({"ok": True, "result": result})
            except Exception as exc:
                error = ("guarded platform operation failed" if guarded
                         else f"{type(exc).__name__}: {str(exc)[:4000]}")
                write({"ok": False, "error": error})
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
