"""File-backed state (AGENT_DESIGN.md §5.4): atomic JSON state,
pre-registration hashing/locking, stop flag.

Processes hold no unique state in memory: everything needed to resume
lives in these files (NFR-01).
"""
import hashlib
import json
import os
from pathlib import Path

from . import paths


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# --- research state -------------------------------------------------------

def research_state_path() -> Path:
    return paths.state_dir() / "research_state.json"


def load_research_state() -> dict:
    return read_json(research_state_path(), default={
        "current_dataset": None,
        "dataset_state": None,
        "selection_mode": None,
        "locked": False,
        "current_batch": None,      # batch timestamp prefix
        "auth_mode": "auto",        # FR-12 default: automatic authorization
        "alphas": {},               # alpha_id -> alpha state (see gatekeeper)
    })


def save_research_state(state: dict) -> None:
    atomic_write_json(research_state_path(), state)


# --- pre-registration locking (FR-06 / design §6.4) ------------------------

def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def lock_prereg(prereg_path: Path, state: dict) -> str:
    """Record the prereg hash in state; later edits become detectable."""
    digest = file_sha256(prereg_path)
    state.setdefault("prereg_hashes", {})[str(prereg_path)] = digest
    return digest


def verify_prereg(prereg_path: Path, state: dict) -> bool:
    recorded = state.get("prereg_hashes", {}).get(str(prereg_path))
    return recorded is not None and recorded == file_sha256(prereg_path)


# --- stop flag (FR-12 / design §6.5) ---------------------------------------

def stop_flag_path() -> Path:
    return paths.state_dir() / "stop.flag"


def set_stop() -> None:
    stop_flag_path().parent.mkdir(parents=True, exist_ok=True)
    stop_flag_path().write_text("stop\n", encoding="utf-8")


def clear_stop() -> None:
    stop_flag_path().unlink(missing_ok=True)


def stop_requested() -> bool:
    return stop_flag_path().exists()
