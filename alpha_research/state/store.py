"""File-backed state (AGENT_DESIGN.md §5.4): atomic JSON state,
pre-registration hashing/locking, stop flag.

Processes hold no unique state in memory: everything needed to resume
lives in these files (NFR-01).
"""
import hashlib
import json
import os
from pathlib import Path

from alpha_research.state import paths
from alpha_research.state.operation_lock import SubmissionGate


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


def is_pre_submission_draft(batch_dir: Path) -> bool:
    """Only batches with no Regular or diagnostic submission evidence are drafts."""
    try:
        names = {path.name for path in batch_dir.iterdir()}
    except OSError:
        return False
    return not (
        names.intersection({"simulation_intent.json", "simulation_raw.json",
                            "results.json", "gate.json"})
        or any(name.startswith("probe_") and name.endswith(("_intent.json", "_raw.json"))
               for name in names)
    )


def has_only_completed_probes(batch_dir: Path) -> bool:
    """Verify that every diagnostic submission is settled and production has not started."""
    try:
        names = {path.name for path in batch_dir.iterdir()}
    except OSError:
        return False
    if names.intersection({"simulation_intent.json", "simulation_raw.json",
                           "results.json", "gate.json", "void.json"}):
        return False
    plan = read_json(batch_dir / "probe_plan.json")
    probes = plan.get("probes") if isinstance(plan, dict) else None
    if (not isinstance(probes, list) or not probes or
            any(not isinstance(probe, dict) or not isinstance(probe.get("expression"), str)
                or not probe["expression"].strip() for probe in probes)):
        return False
    evidence_files = {name for name in names
                      if name.startswith("probe_") and name.endswith(("_intent.json", "_raw.json"))}
    for number, offset in enumerate(range(0, len(probes), 8), start=1):
        intent_name = f"probe_{number}_intent.json"
        raw_name = f"probe_{number}_raw.json"
        expressions = [probe["expression"] for probe in probes[offset:offset + 8]]
        intent = read_json(batch_dir / intent_name)
        raw = read_json(batch_dir / raw_name)
        rows = raw.get("alpha_results") if isinstance(raw, dict) else None
        if (not isinstance(intent, dict) or intent.get("status") != "READ_BACK"
                or intent.get("expressions") != expressions
                or not isinstance(rows, list) or len(rows) != len(expressions)):
            return False
        alpha_ids = set()
        for expression, row in zip(expressions, rows):
            if (not isinstance(row, dict) or row.get("success") is not True
                    or row.get("expression") != expression
                    or not isinstance(row.get("alpha_id"), str) or not row["alpha_id"].strip()
                    or row["alpha_id"] in alpha_ids):
                return False
            alpha_ids.add(row["alpha_id"])
        evidence_files.difference_update((intent_name, raw_name))
    return not evidence_files


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


def update_stop_flag(path: Path, stopped: bool) -> None:
    path = Path(path)
    with SubmissionGate(path.parent):
        if stopped:
            path.write_text("stop\n", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)


def set_stop() -> None:
    update_stop_flag(stop_flag_path(), True)


def clear_stop() -> None:
    update_stop_flag(stop_flag_path(), False)


def stop_requested() -> bool:
    return stop_flag_path().exists()
