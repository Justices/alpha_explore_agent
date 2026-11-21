"""Explicit, fail-closed separation of offline domain mock workspaces.

The marker is an audit marker, never a production authorization token.
A fresh workspace must have no existing domain evidence or legacy runs.
Existing marked workspaces may only be reused for additional offline mocks.
"""
import json
from pathlib import Path

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.state.store import atomic_write_json

MARKER = "domain_mock_workspace.json"
SCHEMA_VERSION = 1
EVIDENCE_FOLDERS = (
    "candidate", "preferred", "candidate_branch", "optimization_source",
    "super_run", "campaign", "dataset_subtask", "combination",
)


def marker_path(state_root):
    return Path(state_root).resolve() / "state" / "research_v1_1" / MARKER


def inspect_mock_workspace(state_root):
    root = Path(state_root).resolve()
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
        if candidate.is_symlink() or (candidate.is_dir() and any(candidate.iterdir())):
            raise OrchestrationError(
                "state-root has domain evidence; use an isolated offline mock root")
    if (root / "runs").is_symlink() or (
            (root / "runs").is_dir() and any((root / "runs").iterdir())):
        raise OrchestrationError(
            "state-root has existing research runs; use an isolated offline mock root")
    return {"status": "FRESH_EMPTY_MOCK_ROOT", "marker": str(path)}


def claim_mock_workspace(state_root):
    report = inspect_mock_workspace(state_root)
    if report["status"] == "FRESH_EMPTY_MOCK_ROOT":
        path = marker_path(state_root)
        atomic_write_json(path, {
            "schema_version": SCHEMA_VERSION,
            "kind": "OFFLINE_DOMAIN_MOCK_ONLY",
            "canonical_state_root": str(Path(state_root).resolve()),
        })
    return report


def is_mock_workspace(state_root):
    return marker_path(state_root).is_file() and (
        inspect_mock_workspace(state_root)["status"] == "EXISTING_MOCK_ONLY")
