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

    def recover(self, target, *, gateway, agent=None, config=None, expected_workflow=None,
                task_id=None, step=None):
        """Recover one existing operation without ever issuing a new Simulation.

        Regular targets are batch directories. Super and Optimization targets
        are their simulation_intent.json files. All targets must live below the
        configured state root.
        """
        path = self._resolve_target(target)
        workflow, resolved, intent, fingerprint = self._inspect(path)
        if expected_workflow is not None and expected_workflow != workflow:
            raise RecoveryError(
                f"recovery target is {workflow}, not requested {expected_workflow}"
            )
        if workflow == "regular" and agent is None:
            raise RecoveryError("Regular recovery requires the existing ResearchAgent")
        if workflow in {"super", "optimization"} and gateway is None:
            raise RecoveryError("Super/Optimization recovery requires the platform gateway")
        if (task_id is None) != (step is None):
            raise RecoveryError("task_id and step must be supplied together")
        if task_id is not None and step != "execute":
            raise RecoveryError("workflow recovery can reconcile only the execute step")
        journal_path = self._journal_path(workflow, resolved)
        if journal_path.exists():
            journal = self._read_json(journal_path, "recovery journal")
            if (journal.get("target_fingerprint") != fingerprint
                    or journal.get("task_id") != task_id
                    or journal.get("step") != step):
                raise RecoveryError(
                    "recovery target evidence or task binding changed; manual review required"
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
            "task_id": task_id,
            "step": step,
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

    def recover_task(self, orchestrator, task_id, *, gateway, agent=None, config=None):
        """Recover a blocked orchestration execute step from its bound domain artifact."""
        task = orchestrator.load(task_id)
        step = (task.get("failure") or {}).get("step")
        target = task.get("recovery_target") or {}
        if (task.get("status") != "BLOCKED" or step != "execute"
                or task.get("steps", {}).get("execute") != "RUNNING"):
            raise RecoveryError("only a blocked execute step can use workflow recovery")
        if not target.get("ref") or not target.get("kind"):
            raise RecoveryError("task has no durable workflow recovery target")
        expected_kind = "regular_batch" if task["workflow"] == "regular" else "simulation_intent"
        if target["kind"] != expected_kind:
            raise RecoveryError("task recovery target kind does not match its workflow")

        result = self.recover(
            target["ref"], gateway=gateway, agent=agent, config=config,
            expected_workflow=task["workflow"], task_id=task_id, step=step)
        if task["workflow"] == "regular":
            request = task["request"]
            if not request.get("objective") or not request.get("region"):
                raise RecoveryError("Regular task request lacks objective or Region for Campaign reconciliation")
            from .regular.campaign import run_regular_campaign
            result = run_regular_campaign(
                agent, gateway, objective=request["objective"], region=request["region"],
                dataset_ids=request.get("dataset_ids"), category=request.get("category"),
                delay=request.get("delay"), universe=request.get("universe"),
                max_batches=request.get("max_batches", 1),
                ppa_profile=bool(request.get("ppa_profile", False)),
                recovery_only=True)

        journal = self.journal_for(target["ref"])
        if (not isinstance(journal, dict) or journal.get("status") != "COMPLETED"
                or journal.get("task_id") != task_id or journal.get("step") != step):
            raise RecoveryError("domain recovery journal is not verified for this task")
        receipt_root = self.journal_root / "tasks"
        receipt_path = receipt_root / f"{task_id}.json"
        receipt_root.mkdir(parents=True, exist_ok=True)
        receipt = {
            "schema_version": 1, "status": "COMPLETED",
            "task_id": task_id, "workflow": task["workflow"], "step": step,
            "target_kind": target["kind"], "target_ref": target["ref"],
            "recovery_id": journal["recovery_id"],
            "recovery_journal_ref": str(self._journal_path(
                task["workflow"], Path(target["ref"]).resolve())),
            "outcome": result, "outcome_sha256": _canonical_sha256(result),
            "created_at": _now(),
        }
        atomic_write_json(receipt_path, receipt)
        receipt_sha256 = _sha256_bytes(receipt_path.read_bytes())
        return orchestrator.reconcile(
            task_id, step, result, receipt_ref=str(receipt_path),
            receipt_sha256=receipt_sha256)
