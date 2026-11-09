"""Offline-first main research orchestration with durable checkpoints.

This control plane never invokes a platform gateway. Adapters are injected for
offline/mock execution; production execution requires a separately reviewed
integration and explicit authorization.
"""
import copy
import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

from ..state_store import atomic_write_json
from .candidate_pool import CandidatePool, policy_hash
from .records import ResearchRecordStore

WORKFLOWS = ("regular", "super", "optimization")
STEPS = ("validate", "execute", "qualify", "collect")
TERMINAL = {"COMPLETED", "BLOCKED", "FAILED", "CANCELLED"}
STEP_STATES = {"PENDING", "RUNNING", "DONE", "BLOCKED"}
SCHEMA_VERSION = 1


def _now(previous=None):
    now = datetime.now(timezone.utc)
    if previous:
        prior = datetime.fromisoformat(previous)
        if now <= prior:
            now = prior + timedelta(microseconds=1)
    return now.isoformat()


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class OrchestrationError(ValueError):
    pass


class MainOrchestrator:
    """Single-writer orchestration; caller must hold state/agent.lock.

    The adapter interface is run(workflow, step, request, checkpoint) -> dict.
    The default adapter is deliberately absent: no accidental live calls.
    """
    def __init__(self, state_root, *, adapter=None, config=None):
        self.state_root = Path(state_root).resolve()
        self.root = self.state_root / "state" / "research_v1_1" / "orchestration"
        self.adapter = adapter
        self.config = config or {}

    def _path(self, task_id):
        if (not isinstance(task_id, str) or len(task_id) != 32 or
                any(ch not in "0123456789abcdef" for ch in task_id)):
            raise OrchestrationError("invalid task ID")
        return self.root / f"{task_id}.json"

    def load(self, task_id):
        path = self._path(task_id)
        task = json.loads(path.read_text(encoding="utf-8"))
        if task.get("task_id") != task_id or task.get("schema_version") != SCHEMA_VERSION:
            raise OrchestrationError("orchestration record identity/schema mismatch")
        if task.get("request_sha256") != _hash(task.get("request")):
            raise OrchestrationError("orchestration request was changed")
        return task

    def _save(self, task, *, expected=None):
        path = self._path(task["task_id"])
        if expected is None and path.exists():
            raise OrchestrationError("task already exists")
        if expected is not None:
            current = self.load(task["task_id"])
            if current["updated_at"] != expected:
                raise OrchestrationError("concurrent task update")
        task["updated_at"] = _now(expected)
        atomic_write_json(path, task)
        return copy.deepcopy(task)

    def create(self, workflow, request, *, task_id=None, budget=1):
        if workflow not in WORKFLOWS:
            raise OrchestrationError("unknown workflow")
        if not isinstance(request, dict) or not isinstance(request.get("objective"), str) or not request["objective"].strip():
            raise OrchestrationError("objective is required")
        if workflow == "optimization" and not request.get("alpha_id"):
            raise OrchestrationError("optimization requires source Alpha ID")
        if type(budget) is not int or budget < 1:
            raise OrchestrationError("budget must be positive")
        task_id = task_id or uuid4().hex
        now = _now()
        task = {
            "schema_version": SCHEMA_VERSION, "task_id": task_id,
            "workflow": workflow, "request": copy.deepcopy(request),
            "request_sha256": _hash(request), "status": "CREATED",
            "steps": {step: "PENDING" for step in STEPS},
            "checkpoints": {}, "result": None, "failure": None, "recovery_target": None,
            "budget": {"limit": budget, "used": 0, "reserved": 0},
            "created_at": now, "updated_at": now,
        }
        return self._save(task)

    def _transition(self, task, **changes):
        prior = task["updated_at"]
        task.update(changes)
        return self._save(task, expected=prior)

    def bind_recovery_target(self, task_id, kind, reference):
        """Persist the exact workflow artifact needed to recover an execute step."""
        task = self.load(task_id)
        if task.get("status") != "RUNNING" or task.get("steps", {}).get("execute") != "RUNNING":
            raise OrchestrationError("recovery target can only be bound during execute")
        expected_kind = "regular_batch" if task["workflow"] == "regular" else "simulation_intent"
        if kind != expected_kind:
            raise OrchestrationError("recovery target kind does not match workflow")
        path = Path(reference).expanduser().resolve()
        if not path.is_relative_to(self.state_root) or not path.exists():
            raise OrchestrationError("recovery target must exist under the configured state root")
        if kind == "regular_batch":
            if not path.is_dir() or not (path / "simulation_intent.json").is_file():
                raise OrchestrationError("Regular recovery target must be a batch directory with an intent")
        else:
            if not path.is_file():
                raise OrchestrationError("simulation intent recovery target must be a file")
            try:
                intent = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise OrchestrationError("simulation intent is unreadable") from exc
            if intent.get("workflow") != task["workflow"]:
                raise OrchestrationError("simulation intent workflow does not match task")
        return self._transition(task, recovery_target={
            "kind": kind, "ref": str(path), "bound_at": _now()})

    def execute(self, task_id):
        if self.adapter is None:
            raise OrchestrationError("offline adapter is required; live dispatch is disabled")
        task = self.load(task_id)
        if task["status"] in TERMINAL:
            return task
        for step in STEPS:
            if task["steps"][step] == "DONE":
                continue
            if task["steps"][step] == "RUNNING":
                task = self._transition(task, status="BLOCKED",
                                        failure={"step": step, "reason": "outcome_unknown_manual_reconciliation_required"})
                return task
            if step == "execute" and task["budget"]["used"] + task["budget"]["reserved"] >= task["budget"]["limit"]:
                return self._transition(task, status="BLOCKED",
                                        failure={"step": step, "reason": "budget_exhausted"})
            task["steps"][step] = "RUNNING"
            if step == "execute":
                task["budget"]["reserved"] += 1
            task = self._transition(task, status="RUNNING")
            try:
                context = {
                    "task_id": task_id,
                    "bind_recovery_target": lambda kind, reference:
                        self.bind_recovery_target(task_id, kind, reference),
                }
                outcome = self.adapter.run(task["workflow"], step,
                                           copy.deepcopy(task["request"]),
                                           copy.deepcopy(task["checkpoints"]),
                                           context=context)
                if not isinstance(outcome, dict):
                    raise OrchestrationError("adapter must return a JSON object")
                # Checkpoint must be durable and serializable before marking DONE.
                json.dumps(outcome, allow_nan=False)
                task["checkpoints"][step] = outcome
                task["steps"][step] = "DONE"
                if step == "execute":
                    task["budget"]["reserved"] -= 1
                    task["budget"]["used"] += 1
                task = self._transition(task, status="RUNNING", failure=None)
            except Exception as exc:
                # Fail closed: RUNNING execute has unknown external outcome.
                # Never auto-replay it or refund a reserved budget.
                reason = "outcome_unknown_manual_reconciliation_required" if step == "execute" else type(exc).__name__
                task = self._transition(task, status="BLOCKED",
                                        failure={"step": step, "reason": reason})
                return task
        task = self._transition(task, status="COMPLETED",
                                result=copy.deepcopy(task["checkpoints"].get("collect")))
        return task

    def resume(self, task_id):
        task = self.load(task_id)
        if task["status"] == "BLOCKED":
            raise OrchestrationError("blocked task requires explicit reconciliation; no automatic replay")
        return self.execute(task_id)

    def reconcile(self, task_id, step, outcome):
        """Record independently verified completion, never perform platform operations."""
        task = self.load(task_id)
        if (task["status"] != "BLOCKED" or task.get("failure", {}).get("step") != step
                or task["steps"].get(step) != "RUNNING"):
            raise OrchestrationError("only blocked running step can be reconciled")
        if not isinstance(outcome, dict):
            raise OrchestrationError("verified outcome must be a JSON object")
        json.dumps(outcome, allow_nan=False)
        task["checkpoints"][step] = copy.deepcopy(outcome)
        task["steps"][step] = "DONE"
        if step == "execute":
            task["budget"]["reserved"] -= 1
            task["budget"]["used"] += 1
        return self._transition(task, status="RUNNING", failure=None)


class OfflineResearchAdapter:
    """Explicit fixture adapter; cannot call the platform or submit Alphas."""
    def __init__(self, fixtures):
        if not isinstance(fixtures, dict):
            raise OrchestrationError("fixtures must be an object")
        self.fixtures = fixtures

    def run(self, workflow, step, request, checkpoints, context=None):
        key = workflow + "." + step
        if key not in self.fixtures:
            raise OrchestrationError("missing offline fixture: " + key)
        result = self.fixtures[key]
        if not isinstance(result, dict):
            raise OrchestrationError("offline fixture must be an object")
        if step == "collect":
            result = copy.deepcopy(result)
            result["qualification"] = checkpoints.get("qualify")
        return copy.deepcopy(result)


def pool_snapshot(state_root, config):
    """P3: read-only unified pool projection; never promotes a candidate."""
    return CandidatePool(ResearchRecordStore(state_root)).build_index(
        current_policy_sha256=policy_hash(config))
