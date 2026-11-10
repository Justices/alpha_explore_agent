"""Optional launch progress for the Workbench. Callers supply safe text only."""

from datetime import datetime
import json
import os
from pathlib import Path


PROGRESS_ENV = "ALPHA_RESEARCH_PROGRESS_FILE"


def write_progress(message, path=None):
    path = path or os.environ.get(PROGRESS_ENV)
    if not path:
        return
    event = {"time": datetime.now().astimezone().isoformat(timespec="seconds"),
             "message": message}
    try:
        with Path(path).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        # An unavailable log must never change a platform operation's outcome.
        pass
