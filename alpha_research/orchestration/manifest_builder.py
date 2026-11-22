"""Persist a single Agent-produced Simulation manifest before any POST.

This is an internal orchestration handoff, not a user supplied JSON file.
The producer supplies a validated Research candidate in memory; the builder
binds it to an unstarted Task and saves an immutable fingerprinted artifact.
No model, BRAIN, scope issuer or operator issuer is called here. Generated
artifacts are local evidence only and never dispatch permissions.
"""
import copy
import json
import re
from pathlib import Path

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.simulation_scope import _validate_plan
from alpha_research.state.fingerprint import fingerprint_json
from alpha_research.state.store import atomic_write_json
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError

_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_VERSION = 1
_STATUS = "GENERATED_LOCAL_NOT_AUTHORIZED"


def _path(orchestrator, task_id):
    orchestrator._path(task_id)
    return (Path(orchestrator.state_root) / "state" / "research_v1_1" /
            "generated_simulation_manifests" / (task_id + ".json"))


def _fresh(task):
    return (task.get("status") == "CREATED"
            and task.get("workflow") in {"super", "optimization"}
            and task.get("budget") == {"limit": 1, "used": 0, "reserved": 0}
            and task.get("research_identity") is None
            and task.get("recovery_target") is None
            and task.get("checkpoints") == {})


def _validate_candidate(task, candidate):
    if not _fresh(task):
        raise OrchestrationError("Agent manifest requires a fresh unexecuted Task")
    if not isinstance(candidate, dict) or set(candidate) != {
            "run_id", "batch_id", "request", "settings"}:
        raise OrchestrationError("Agent output must contain run_id, batch_id, request and settings")
    rid = candidate["run_id"]
    if not isinstance(rid, str) or _HEX32.fullmatch(rid) is None:
        raise OrchestrationError("Agent run_id must be 32 lowercase hex characters")
    if (not isinstance(candidate["batch_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", candidate["batch_id"])):
        raise OrchestrationError("Agent batch_id invalid")
    if not isinstance(candidate["request"], dict) or not candidate["request"]:
        raise OrchestrationError("Agent candidate request is empty")
    if task["workflow"] == "super":
        for field in ("selection", "combo"):
            value = candidate["request"].get(field)
            if not isinstance(value, str) or not value.strip():
                raise OrchestrationError("Super candidate missing " + field)
    elif not (
        isinstance(candidate["request"].get("selection"), str)
        and isinstance(candidate["request"].get("combo"), str)
    ) and not (
        isinstance(candidate["request"].get("variants"), list)
        and candidate["request"]["variants"]
        and candidate["request"].get("origin_alpha_id") == task["request"].get("alpha_id")
    ):
        raise OrchestrationError("Optimization candidate source/expression invalid")
    manifest = {
        "research_id": task["workflow"] + ":" + rid,
        "run_id": rid,
        "batch_id": candidate["batch_id"],
        "request": copy.deepcopy(candidate["request"]),
        "settings": copy.deepcopy(candidate["settings"]),
    }
    _validate_plan(task, manifest)
    return manifest


def read_generated_manifest(orchestrator, task_id):
    """Return validated internal manifest or None; refuse damaged or orphaned."""
    task = orchestrator.load(task_id)
    path = _path(orchestrator, task_id)
    root = path.parent
    if (root.is_symlink() or path.is_symlink()
            or (root.exists() and not root.is_dir())):
        raise OrchestrationError("generated manifest path unsafe")
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or set(record) != {
                "schema_version", "task_id", "workflow", "task_request_sha256",
                "manifest", "manifest_sha256", "status", "permission_to_dispatch"}
                or type(record["schema_version"]) is not int
                or record["schema_version"] != _VERSION
                or record["task_id"] != task_id
                or record["workflow"] != task["workflow"]
                or record["task_request_sha256"] != task["request_sha256"]
                or record["status"] != _STATUS
                or record["permission_to_dispatch"] is not False
                or not isinstance(record["manifest"], dict)
                or fingerprint_json(record["manifest"]) != record["manifest_sha256"]):
            raise OrchestrationError("generated manifest identity or digest drift")
        manifest = record["manifest"]
        expected = _validate_candidate(task, {
            "run_id": manifest["run_id"],
            "batch_id": manifest["batch_id"],
            "request": manifest["request"], "settings": manifest["settings"],
        })
        if manifest != expected:
            raise OrchestrationError("generated manifest changed")
        return copy.deepcopy(manifest)
    except OrchestrationError:
        raise
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise OrchestrationError("generated manifest missing, corrupt or unsafe") from exc


def record_agent_generated_manifest(orchestrator, task_id, candidate):
    """Accept validated Agent output in-process, once per fresh Task.

    The caller must independently validate Alpha expressions and research
    provenance. This builder enforces shape, scope and durability but cannot
    establish model provenance or authorize a real Simulation.
    """
    path = _path(orchestrator, task_id)
    root = path.parent
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise OrchestrationError("generated manifest root unsafe")
    try:
        with SingleWriterLock(path.with_suffix(".lock")):
            task = orchestrator.load(task_id)
            manifest = _validate_candidate(task, candidate)
            if path.exists() or path.is_symlink():
                saved = read_generated_manifest(orchestrator, task_id)
                if saved != manifest:
                    raise OrchestrationError("Agent manifest already pinned; cannot replace")
                return inspect_generated_manifest(orchestrator, task_id)
            record = {
                "schema_version": _VERSION,
                "task_id": task_id,
                "workflow": task["workflow"],
                "task_request_sha256": task["request_sha256"],
                "manifest": manifest,
                "manifest_sha256": fingerprint_json(manifest),
                "status": _STATUS,
                "permission_to_dispatch": False,
            }
            atomic_write_json(path, record)
            return inspect_generated_manifest(orchestrator, task_id)
    except WriterLockError as exc:
        raise OrchestrationError("another agent is publishing this manifest") from exc


def inspect_generated_manifest(orchestrator, task_id):
    """Read-only metadata; never print Alpha expressions or credentials."""
    manifest = read_generated_manifest(orchestrator, task_id)
    if manifest is None:
        return {
            "schema_version": _VERSION, "task_id": task_id,
            "status": "AWAITING_RESEARCH_OUTPUT",
            "next_required": "AGENT_VALIDATED_SIMULATION_CANDIDATE",
            "permission_to_dispatch": False,
            "brain_simulation_authorized": False,
            "model_call_authorized": False,
            "brain_invoked": False, "provider_invoked": False,
        }
    return {
        "schema_version": _VERSION, "task_id": task_id,
        "status": _STATUS,
        "workflow": orchestrator.load(task_id)["workflow"],
        "manifest_sha256": fingerprint_json(manifest),
        "scope": {key: manifest["settings"][key] for key in
                  ("region", "delay", "universe", "instrument_type")},
        "research_id": manifest["research_id"],
        "run_id": manifest["run_id"], "batch_id": manifest["batch_id"],
        "permission_to_dispatch": False,
        "brain_simulation_authorized": False,
        "model_call_authorized": False,
        "brain_invoked": False, "provider_invoked": False,
    }
