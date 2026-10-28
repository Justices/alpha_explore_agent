"""Line-delimited JSON bridge to brain-flow scripts and brain_session.py."""

import json
import sys
from pathlib import Path

PROJECT_SRC = Path(__file__).resolve().parents[1]
if str(PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(PROJECT_SRC))

from nastu_quant.brain_flow_runtime import BrainFlowRuntime  # noqa: E402


def write(payload):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def main():
    try:
        runtime = BrainFlowRuntime()
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
