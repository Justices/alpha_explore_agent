"""Unified, fail-closed recovery coordination for Regular, Super and Optimization.

The coordinator is deliberately not a simulation runner. It only resumes
operations that already have durable evidence that submission began, or
finishes downstream work from a verified READ_BACK receipt. It never creates
a new simulation request.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from ..state_store import atomic_write_json


class RecoveryError(ValueError):
    """A recovery target is invalid, ambiguous, or requires manual review."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return _sha256_bytes(raw)


class RecoveryCoordinator:
    """Single-writer coordinator for existing workflow-specific recovery paths.

    The caller must hold the project's SingleWriterLock. A durable journal is
    written before recovery starts. An interrupted or failed recovery is
    blocked rather than replayed automatically; a completed recovery returns
    its saved result without invoking a workflow handler again.
    """

    REGULAR_SUBMITTING = {"SUBMITTING", "READ_BACK"}
    SIMULATION_RECOVERABLE = {
        "POSTING", "OUTCOME_UNKNOWN", "RESPONSE_RECEIVED", "READ_BACK"
    }

    def __init__(self, state_root, *, simulation_recover=None):
        self.state_root = Path(state_root).resolve()
        self.journal_root = (
            self.state_root / "state" / "research_v1_1" / "recoveries"
        )
        self._simulation_recover = simulation_recover

    def _resolve_target(self, target):
        path = Path(target).expanduser().resolve()
        if not path.is_relative_to(self.state_root):
            raise RecoveryError("recovery target is outside the configured state root")
        if not path.exists():
            raise RecoveryError("recovery target does not exist")
        return path

    @staticmethod
    def _read_json(path, label):
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RecoveryError(f"{label} is missing or unreadable") from exc
        if not isinstance(value, dict):
            raise RecoveryError(f"{label} must contain a JSON object")
        return value

    def _inspect(self, path):
        if path.is_dir():
            intent_path = path / "simulation_intent.json"
            intent = self._read_json(intent_path, "Regular simulation intent")
            status = intent.get("status")
            if status not in self.REGULAR_SUBMITTING:
                raise RecoveryError(
                    "Regular recovery is allowed only after submission began "
                    "or after READ_BACK; PREPARED intents are never submitted by recovery"
                )
            if status == "READ_BACK" and not (path / "results.json").is_file():
                raise RecoveryError("Regular READ_BACK intent has no saved results")
            prereg_path = path / "prereg.json"
            prereg_digest = (
                _sha256_bytes(prereg_path.read_bytes())
                if prereg_path.is_file() else None
            )
            immutable = {
                "workflow": "regular",
                "batch_dir": str(path),
                "expressions": intent.get("expressions"),
                "settings": intent.get("settings"),
                "runner": intent.get("runner"),
                "prereg_sha256": intent.get("prereg_sha256"),
                "prereg_file_sha256": prereg_digest,
            }
            return "regular", path, intent, _canonical_sha256(immutable)

        intent = self._read_json(path, "simulation intent")
        workflow = intent.get("workflow")
        if workflow not in {"super", "optimization"}:
            raise RecoveryError(
                "file recovery target must be a Super or Optimization simulation intent"
            )
        if intent.get("status") not in self.SIMULATION_RECOVERABLE:
            raise RecoveryError(
                "simulation intent is not recoverable; PREPARED intents cannot be submitted"
            )
        request = intent.get("request")
        settings = intent.get("settings")
        if not isinstance(request, dict) or not isinstance(settings, dict):
            raise RecoveryError("simulation intent request/settings are invalid")
        immutable = {
            "schema_version": intent.get("schema_version"),
            "workflow": workflow,
            "run_id": intent.get("run_id"),
            "batch_id": intent.get("batch_id"),
            "request_sha256": intent.get("request_sha256"),
            "request": request,
            "settings": settings,
            "metadata": intent.get("metadata") or {},
        }
        return workflow, path, intent, _canonical_sha256(immutable)

    def _journal_path(self, workflow, target):
        identity = _canonical_sha256({
            "workflow": workflow,
            "target": str(target),
        })
        return self.journal_root / f"{identity}.json"

    def journal_for(self, target):
        """Return the durable journal for a target, if one has been created."""
        path = self._resolve_target(target)
        workflow, _, _, _ = self._inspect(path)
        journal_path = self._journal_path(workflow, path)
        if not journal_path.exists():
            return None
        return self._read_json(journal_path, "recovery journal")

    def recover(self, target, *, gateway, agent=None, config=None):
        """Recover one existing operation without ever issuing a new Simulation.

        Regular targets are batch directories. Super and Optimization targets
        are their simulation_intent.json files. All targets must live below the
        configured state root.
        """
        path = self._resolve_target(target)
        workflow, resolved, intent, fingerprint = self._inspect(path)
        journal_path = self._journal_path(workflow, resolved)
        if journal_path.exists():
            journal = self._read_json(journal_path, "recovery journal")
            if journal.get("target_fingerprint") != fingerprint:
                raise RecoveryError(
                    "recovery target evidence changed after the first attempt; manual review required"
                )
            if journal.get("status") == "COMPLETED":
                result = journal.get("result")
                if (not isinstance(result, dict)
                        or journal.get("result_sha256") != _canonical_sha256(result)):
                    raise RecoveryError("completed recovery journal result is corrupt")
                return result
            raise RecoveryError(
                "recovery is already RUNNING or BLOCKED; automatic replay is prohibited"
            )

        now = _now()
        journal = {
            "schema_version": 1,
            "recovery_id": journal_path.stem,
            "workflow": workflow,
            "target_ref": str(resolved),
            "target_fingerprint": fingerprint,
            "status": "RUNNING",
            "started_at": now,
            "updated_at": now,
            "result": None,
            "result_sha256": None,
            "failure": None,
        }
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(journal_path, journal)

        try:
            if workflow == "regular":
                if agent is None:
                    raise RecoveryError("Regular recovery requires the existing ResearchAgent")
                result = agent.recover(resolved)
            else:
                recover_fn = self._simulation_recover
                if recover_fn is None:
                    from .simulation_intent import recover_simulation_intent
                    recover_fn = recover_simulation_intent
                result = recover_fn(gateway, self.state_root, resolved, config=config)
            if not isinstance(result, dict):
                raise RecoveryError("workflow recovery returned a non-object result")
            result_digest = _canonical_sha256(result)
            journal.update(
                status="COMPLETED",
                result=result,
                result_sha256=result_digest,
                completed_at=_now(),
                updated_at=_now(),
            )
            atomic_write_json(journal_path, journal)
            return result
        except Exception as exc:
            journal.update(
                status="BLOCKED",
                failure={
                    "type": type(exc).__name__,
                    "message": str(exc)[:500],
                },
                updated_at=_now(),
            )
            atomic_write_json(journal_path, journal)
            if isinstance(exc, RecoveryError):
                raise
            raise RecoveryError(
                f"{workflow} recovery failed and is blocked for manual review: "
                f"{type(exc).__name__}"
            ) from exc
