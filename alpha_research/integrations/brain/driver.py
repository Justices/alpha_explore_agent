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


def write(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def main():
    args = sys.argv[1:]
    read_only = "--read-only" in args
    guarded = "--orchestration-guarded" in args
    flags = {"--read-only", "--orchestration-guarded"}
    remaining = [arg for arg in args if arg not in flags]
    if (len(remaining) != len(args) - int(read_only) - int(guarded)
            or remaining not in ([], ["--brain-credentials-stdin"])):
        write({"ok": False, "stage": "configuration",
               "error": "invalid driver options"})
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
