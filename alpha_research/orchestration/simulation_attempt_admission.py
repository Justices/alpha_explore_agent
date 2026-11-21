"""P4.5.2 crash-conservative Simulation attempt staging (NO dispatch grant).

An externally signed scoped receipt can be verified and its identity staged
once. The journal is written FIRST, ahead of a local Simulation Intent.
A crash at any point must never permit an automatic second staging or POST.

This module does not establish consent, issue a signed receipt, create a
platform session or return an executable capability. Production dispatch
remains disabled until a separately reviewed trusted issuer/consumer exists.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.mock_workspace import marker_path
from alpha_research.orchestration.simulation_scope import inspect_simulation_scope
from alpha_research.simulation.intent import create_simulation_intent
from alpha_research.state.fingerprint import fingerprint_json
from alpha_research.state.store import atomic_write_json
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError

_VERSION = 1
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


def inspect_staged_simulation_attempt(orchestrator, task_id):
    """Read-only state; no gateway, receipt issuer, consent or model."""
    journal_path, intent_path, _ = _paths(orchestrator, task_id)
    if not journal_path.exists():
        if intent_path.exists() or intent_path.is_symlink():
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
                    "workflow", "research_id", "simulation_sha256",
                    "receipt_sha256", "intent_ref", "status",
                    "permission_to_dispatch", "created_at"}
                or type(record["schema_version"]) is not int
                or record["schema_version"] != _VERSION
                or record["task_id"] != task_id
                or record["status"] != _STAGED
                or record["permission_to_dispatch"] is not False
                or record["intent_ref"] != str(intent_path.resolve())):
            raise ValueError("attempt journal malformed")
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        if (not isinstance(intent, dict)
                or intent.get("workflow") != record["workflow"]
                or intent.get("metadata", {}).get("task_id") != task_id
                or intent.get("metadata", {}).get("authorization_id")
                   != record["authorization_id"]
                or intent.get("request_sha256") != record["simulation_sha256"]
                or intent.get("status") != "PREPARED"):
            raise ValueError("Simulation Intent and staging journal disagree")
        return {"task_id": task_id, "status": _STAGED,
                "authorization_id": record["authorization_id"],
                "simulation_sha256": record["simulation_sha256"],
                "permission_to_dispatch": False}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {"task_id": task_id, "status": _RECONCILE,
                "permission_to_dispatch": False}


def stage_simulation_attempt(orchestrator, task_id, model_config,
                             governance_policy, receipt, plan, *,
                             environ=None, now=None):
    """Reserve one receipt/task ID and local Intent, never POST.

    The global state writer lock + a fresh task ensure the journal has
    a single owner. The canonical journal is written before the Intent:
    partial failures are held for manual reconciliation, not retried.
    """
    journal_path, intent_path, lock_path = _paths(orchestrator, task_id)
    marker = marker_path(orchestrator.state_root)
    if marker.exists() or marker.is_symlink():
        raise OrchestrationError("cannot stage Simulation in mock workspace")
    if (Path(orchestrator.state_root) / "state" / "stop.flag").exists():
        raise OrchestrationError("global research stop flag is active")
    checked = inspect_simulation_scope(
        orchestrator, task_id, model_config, governance_policy,
        receipt, plan, environ=environ, now=now)
    try:
        with SingleWriterLock(lock_path):
            # Check all state and signature bindings again under the lock.
            checked = inspect_simulation_scope(
                orchestrator, task_id, model_config, governance_policy,
                receipt, plan, environ=environ, now=now)
            if (journal_path.exists() or journal_path.is_symlink()
                    or intent_path.exists() or intent_path.is_symlink()):
                raise OrchestrationError(
                    "Simulation task already staged; no replay or automatic repair")
            # Detect identical authorization ID already staged for any task,
            # even when the task differs; never reuse a receipt across tasks.
            for candidate in journal_path.parent.glob("*.json"):
                if candidate.is_symlink():
                    raise OrchestrationError("Simulation staging journal symlink detected")
                try:
                    other = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise OrchestrationError(
                        "Simulation staging journal requires reconciliation") from exc
                if not isinstance(other, dict) or not isinstance(
                        other.get("authorization_id"), str):
                    raise OrchestrationError("Simulation staging journal malformed")
                if other["authorization_id"] == checked["authorization_id"]:
                    raise OrchestrationError(
                        "Simulation authorization ID already consumed for staging")
            record = {
                "schema_version": _VERSION, "task_id": task_id,
                "authorization_id": checked["authorization_id"],
                "workflow": checked["workflow"],
                "research_id": checked["research_id"],
                "simulation_sha256": checked["simulation_sha256"],
                "receipt_sha256": fingerprint_json(receipt),
                "intent_ref": str(intent_path.resolve()),
                "status": _STAGED, "permission_to_dispatch": False,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
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
