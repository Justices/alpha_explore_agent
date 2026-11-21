"""P4.5.2 crash-conservative Simulation attempt staging (NO dispatch grant).

An externally signed scoped receipt can be verified and its identity staged
once. The journal is written FIRST, ahead of a local Simulation Intent.
A crash at any point must never permit an automatic second staging or POST.

This module does not establish consent, issue a signed receipt, create a
platform session or return an executable capability. Production dispatch
remains disabled until a separately reviewed trusted issuer/consumer exists.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.mock_workspace import marker_path
from alpha_research.orchestration.simulation_scope import inspect_simulation_scope
from alpha_research.simulation.intent import create_simulation_intent
from alpha_research.state.fingerprint import fingerprint_json
from alpha_research.state.store import atomic_write_json
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError

_VERSION = 2
_INDEX_VERSION = 1
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_STAGED = "STAGED_NO_DISPATCH"
_RECONCILE = "RECONCILIATION_REQUIRED"


def _directory(orchestrator):
    return (Path(orchestrator.state_root) / "state" / "research_v1_1"
            / "simulation_attempt_admission")


def _paths(orchestrator, task_id):
    orchestrator._path(task_id)
    root = _directory(orchestrator)
    return (root / (task_id + ".json"),
            root / "intents" / (task_id + ".intent.json"),
            root / "reservation.lock")



def _load_authorization_index(root):
    """Read the canonical one-use index, never rebuild it from projections.

    A ledger-first crash leaves an unresolvable reservation, not a reusable
    authorization ID. Legacy staging records without an index need manual
    reconciliation, never silent adoption or migration.
    """
    path = root / "authorization_index.json"
    if path.is_symlink():
        raise OrchestrationError("Simulation authorization index is a symlink")
    projections = list(root.glob("[0-9a-f]" * 32 + ".json"))
    intent_dir = root / "intents"
    if not path.exists():
        if projections or intent_dir.is_symlink() or (
                intent_dir.exists() and (
                    not intent_dir.is_dir() or any(intent_dir.iterdir()))):
            raise OrchestrationError(
                "Simulation authorization index missing with durable projections")
        return {"schema_version": _INDEX_VERSION, "by_id": {}, "by_task": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OrchestrationError("Simulation authorization index unreadable") from exc
    if (not isinstance(data, dict)
            or set(data) != {"schema_version", "by_id", "by_task"}
            or type(data["schema_version"]) is not int
            or data["schema_version"] != _INDEX_VERSION
            or not isinstance(data["by_id"], dict)
            or not isinstance(data["by_task"], dict)):
        raise OrchestrationError("Simulation authorization index schema invalid")
    by_id, by_task = data["by_id"], data["by_task"]
    if (len(by_id) != len(by_task)
            or any(not isinstance(aid, str) or _HEX32.fullmatch(aid) is None
                   or not isinstance(tid, str) or _HEX32.fullmatch(tid) is None
                   or by_task.get(tid) != aid
                   for aid, tid in by_id.items())
            or any(by_id.get(aid) != tid for tid, aid in by_task.items())):
        raise OrchestrationError("Simulation authorization reciprocal index mismatch")
    # Every registered task must retain its projection. Missing projections
    # include a crash between the canonical write and the per-task write.
    for aid, tid in by_id.items():
        projected = root / (tid + ".json")
        if projected.is_symlink() or not projected.is_file():
            raise OrchestrationError("Simulation authorization projection missing")
        try:
            body = json.loads(projected.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OrchestrationError("Simulation authorization projection unreadable") from exc
        if (not isinstance(body, dict) or body.get("authorization_id") != aid
                or body.get("task_id") != tid or body.get("status") != _STAGED
                or body.get("permission_to_dispatch") is not False):
            raise OrchestrationError("Simulation authorization projection mismatch")
    if any(candidate.stem not in by_task or candidate.is_symlink()
           for candidate in projections):
        raise OrchestrationError("Simulation authorization orphan projection")
    return data


def inspect_staged_simulation_attempt(orchestrator, task_id):
    """Read-only state; no gateway, receipt issuer, consent or model."""
    journal_path, intent_path, _ = _paths(orchestrator, task_id)
    try:
        index = _load_authorization_index(journal_path.parent)
    except (OrchestrationError, OSError, ValueError, TypeError, KeyError):
        return {"task_id": task_id, "status": _RECONCILE,
                "permission_to_dispatch": False}
    if journal_path.is_symlink():
        return {"task_id": task_id, "status": _RECONCILE,
                "permission_to_dispatch": False}
    if not journal_path.exists():
        if (task_id in index["by_task"]
                or intent_path.exists() or intent_path.is_symlink()):
            return {"task_id": task_id, "status": _RECONCILE,
                    "permission_to_dispatch": False}
        return {"task_id": task_id, "status": "NOT_STAGED",
                "permission_to_dispatch": False}
    try:
        if journal_path.is_symlink() or intent_path.is_symlink():
            raise ValueError("attempt evidence symlink forbidden")
        record = json.loads(journal_path.read_text(encoding="utf-8"))
        if (not isinstance(record, dict)
                or set(record) != {
                    "schema_version", "task_id", "authorization_id",
                    "workflow", "research_id", "run_id", "batch_id",
                    "simulation_sha256", "receipt_sha256", "intent_ref", "status",
                    "permission_to_dispatch", "created_at"}
                or type(record["schema_version"]) is not int
                or record["schema_version"] != _VERSION
                or record["task_id"] != task_id
                or index["by_task"].get(task_id) != record["authorization_id"]
                or index["by_id"].get(record["authorization_id"]) != task_id
                or record["status"] != _STAGED
                or record["permission_to_dispatch"] is not False
                or record["intent_ref"] != str(intent_path.resolve())):
            raise ValueError("attempt journal malformed")
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        if (not isinstance(intent, dict)
                or type(intent.get("schema_version")) is not int
                or intent["schema_version"] != 1
                or intent.get("workflow") != record["workflow"]
                or intent.get("run_id") != record["run_id"]
                or intent.get("batch_id") != record["batch_id"]
                or intent.get("metadata") != {
                    "task_id": task_id,
                    "authorization_id": record["authorization_id"],
                    "research_id": record["research_id"],
                    "dispatch_authorized": False,
                }
                or not isinstance(intent.get("request"), dict)
                or not isinstance(intent.get("settings"), dict)
                or intent.get("request_sha256") != record["simulation_sha256"]
                or fingerprint_json({
                    "request": intent["request"],
                    "settings": intent["settings"],
                }) != record["simulation_sha256"]
                or intent.get("receipt_ref") is not None
                or intent.get("status") != "PREPARED"
                or not isinstance(intent.get("history"), list)
                or len(intent["history"]) != 1
                or not isinstance(intent["history"][0], dict)
                or intent["history"][0].get("status") != "PREPARED"):
            raise ValueError("Simulation Intent and staging journal disagree")
        return {"task_id": task_id, "status": _STAGED,
                "authorization_id": record["authorization_id"],
                "simulation_sha256": record["simulation_sha256"],
                "permission_to_dispatch": False}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"task_id": task_id, "status": _RECONCILE,
                "permission_to_dispatch": False}


def _assert_staging_environment(orchestrator):
    """Check both before receipt inspection and under the reservation lock.

    A stop signal or Mock marker can appear while scope verification runs.
    Neither a dangling symlink nor a concurrent marker may be ignored.
    """
    marker = marker_path(orchestrator.state_root)
    if marker.exists() or marker.is_symlink():
        raise OrchestrationError("cannot stage Simulation in mock workspace")
    stop = Path(orchestrator.state_root) / "state" / "stop.flag"
    if stop.exists() or stop.is_symlink():
        raise OrchestrationError("global research stop flag is active")


def stage_simulation_attempt(orchestrator, task_id, model_config,
                             governance_policy, receipt, plan, *,
                             environ=None, now=None):
    """Reserve one receipt/task ID and local Intent, never POST.

    The global state writer lock + a fresh task ensure the journal has
    a single owner. The canonical journal is written before the Intent:
    partial failures are held for manual reconciliation, not retried.
    """
    journal_path, intent_path, lock_path = _paths(orchestrator, task_id)
    _assert_staging_environment(orchestrator)
    checked = inspect_simulation_scope(
        orchestrator, task_id, model_config, governance_policy,
        receipt, plan, environ=environ, now=now)
    try:
        with SingleWriterLock(lock_path):
            # Check all state and signature bindings again under the lock.
            checked = inspect_simulation_scope(
                orchestrator, task_id, model_config, governance_policy,
                receipt, plan, environ=environ, now=now)
            _assert_staging_environment(orchestrator)
            index = _load_authorization_index(journal_path.parent)
            if (journal_path.exists() or journal_path.is_symlink()
                    or intent_path.exists() or intent_path.is_symlink()
                    or task_id in index["by_task"]):
                raise OrchestrationError(
                    "Simulation task already staged; no replay or automatic repair")
            if checked["authorization_id"] in index["by_id"]:
                raise OrchestrationError(
                    "Simulation authorization ID already consumed for staging")
            record = {
                "schema_version": _VERSION, "task_id": task_id,
                "authorization_id": checked["authorization_id"],
                "workflow": checked["workflow"],
                "research_id": checked["research_id"],
                "run_id": plan["run_id"], "batch_id": plan["batch_id"],
                "simulation_sha256": checked["simulation_sha256"],
                "receipt_sha256": fingerprint_json(receipt),
                "intent_ref": str(intent_path.resolve()),
                "status": _STAGED, "permission_to_dispatch": False,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            # The canonical reciprocal index is published FIRST. Any crash
            # before a complete journal/Intent is RECONCILIATION_REQUIRED;
            # authorization_id and task_id can never be silently reused.
            index["by_id"][checked["authorization_id"]] = task_id
            index["by_task"][task_id] = checked["authorization_id"]
            atomic_write_json(journal_path.parent / "authorization_index.json", index)
            atomic_write_json(journal_path, record)
            create_simulation_intent(
                intent_path, workflow=checked["workflow"],
                run_id=plan["run_id"], batch_id=plan["batch_id"],
                request=plan["request"], settings=plan["settings"],
                metadata={"task_id": task_id,
                          "authorization_id": checked["authorization_id"],
                          "research_id": checked["research_id"],
                          "dispatch_authorized": False})
            return inspect_staged_simulation_attempt(orchestrator, task_id)
    except WriterLockError as exc:
        raise OrchestrationError("Simulation staging lock held by another process") from exc
