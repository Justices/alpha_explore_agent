"""Line-delimited JSON bridge to this project's independent BRAIN runtime."""

import json
import sys
from pathlib import Path

IMPORT_ROOT = Path(__file__).resolve().parents[3]
if str(IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(IMPORT_ROOT))

from alpha_research.integrations.brain.runtime import PlatformRuntime, WorldQuantClient  # noqa: E402


def write(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def main():
    try:
        if sys.argv[1:] == ["--brain-credentials-stdin"]:
            credentials = json.loads(sys.stdin.readline())
            runtime = PlatformRuntime(client=WorldQuantClient(credentials=credentials))
        else:
            runtime = PlatformRuntime()
    except Exception as exc:
        write({"ok": False, "stage": "configuration", "error": str(exc)[:1000]})
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
                params = {key: value for key, value in request.items() if key != "cmd"}
                result = runtime.dispatch(command, params)
                write({"ok": True, "result": result})
            except Exception as exc:
                write({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:4000]}"})
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
