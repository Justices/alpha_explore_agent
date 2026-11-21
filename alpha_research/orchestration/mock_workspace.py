"""Explicit, fail-closed separation of offline domain mock workspaces.

The marker is an audit marker, never a production authorization token.
A fresh workspace must have no existing domain evidence or legacy runs.
Existing marked workspaces may only be reused for additional offline mocks.
"""
import json
from pathlib import Path

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.state.store import atomic_write_json
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError

MARKER = "domain_mock_workspace.json"
SCHEMA_VERSION = 1
EVIDENCE_FOLDERS = (
    "candidate", "preferred", "candidate_branch", "optimization_source",
    "super_run", "campaign", "dataset_subtask", "combination",
    "campaign_runs", "execution_journals", "simulation_intents",
)


def marker_path(state_root):
    return Path(state_root).resolve() / "state" / "research_v1_1" / MARKER


def inspect_mock_workspace(state_root):
    root = Path(state_root).resolve()
    for ancestor in (root / "state", root / "state" / "research_v1_1"):
        if ancestor.is_symlink() or (ancestor.exists() and not ancestor.is_dir()):
            raise OrchestrationError("mock workspace parent directory is unsafe")
    path = marker_path(root)
    if path.is_symlink():
        raise OrchestrationError("mock workspace marker cannot be a symlink")
    if path.exists():
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OrchestrationError("mock workspace marker is unreadable") from exc
        if (not isinstance(body, dict)
                or body != {"schema_version": SCHEMA_VERSION,
                            "kind": "OFFLINE_DOMAIN_MOCK_ONLY",
                            "canonical_state_root": str(root)}):
            raise OrchestrationError("mock workspace identity differs; refusing execution")
        return {"status": "EXISTING_MOCK_ONLY", "marker": str(path)}
    state = root / "state" / "research_v1_1"
    for folder in EVIDENCE_FOLDERS:
        candidate = state / folder
        if candidate.is_symlink() or (
                candidate.exists() and (
                    not candidate.is_dir() or any(candidate.iterdir()))):
            raise OrchestrationError(
                "state-root has domain evidence; use an isolated offline mock root")
    runs = root / "runs"
    if runs.is_symlink() or (
            runs.exists() and (
                not runs.is_dir() or any(runs.iterdir()))):
        raise OrchestrationError(
            "state-root has existing research runs; use an isolated offline mock root")
    if (state / "pool_index.json").exists() or (
            root / "state" / "platform").exists():
        raise OrchestrationError(
            "state-root has existing pool/platform state; mock isolation required")
    return {"status": "FRESH_EMPTY_MOCK_ROOT", "marker": str(path)}


def claim_mock_workspace(state_root):
    """Claim under an OS lock and recheck after acquisition, fail closed."""
    root = Path(state_root).resolve()
    # Do not create lock files until the initial evidence safety preflight.
    inspect_mock_workspace(root)
    path = marker_path(root)
    try:
        with SingleWriterLock(path.with_suffix(".claim.lock")):
            report = inspect_mock_workspace(root)
            if report["status"] == "FRESH_EMPTY_MOCK_ROOT":
                atomic_write_json(path, {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "OFFLINE_DOMAIN_MOCK_ONLY",
                    "canonical_state_root": str(root),
                })
            return report
    except WriterLockError as exc:
        raise OrchestrationError(
            "another process is claiming the offline mock workspace") from exc


def is_mock_workspace(state_root):
    return marker_path(state_root).is_file() and (
        inspect_mock_workspace(state_root)["status"] == "EXISTING_MOCK_ONLY")
