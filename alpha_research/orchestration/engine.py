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

from alpha_research.state.store import atomic_write_json
from alpha_research.state.json_contract import read_strict_object
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError
from alpha_research.state.fingerprint import fingerprint_json as _hash
from alpha_research.qualification.pool import CandidatePool, policy_hash
from alpha_research.state.records import ResearchRecordStore
from alpha_research.orchestration.event_journal import ExecutionEventJournal

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

    def _emit_event(self, task, event_type, *, step=None, payload=None,
                    research_id=None):
        journal = ExecutionEventJournal(self.state_root, task["task_id"])
        return journal.append(
            workflow=task["workflow"], event_type=event_type,
            research_id=(research_id or
                         (task.get("research_identity") or {}).get("research_id")),
            step=step, payload=payload or {})

    @staticmethod
    def _outcome_trace(outcome):
        """Project identifiers and evidence refs, never prompts or raw responses."""
        allowed = (
            "status", "campaign_id", "run_id", "branch_id", "alpha_id",
            "source_alpha_id", "candidate_alpha_id", "candidate_id",
            "candidate_status", "preferred_status", "record_ref", "state_path",
            "report_ref", "simulation_intent_ref", "readback_ref",
            "candidate_ref", "preferred_ref", "batches_completed",
        )
        summary = {key: outcome[key] for key in allowed if key in outcome}
        for key in ("candidate_ref", "preferred_ref", "record_ref", "state_path",
                    "report_ref", "simulation_intent_ref", "readback_ref"):
            if key in summary and not isinstance(summary[key], str):
                summary.pop(key)
        return summary

    def _path(self, task_id):
        if (not isinstance(task_id, str) or len(task_id) != 32 or
                any(ch not in "0123456789abcdef" for ch in task_id)):
            raise OrchestrationError("invalid task ID")
        return self.root / f"{task_id}.json"

    def _mutation_lock_path(self, task_id):
        return self._path(task_id).with_suffix(".write.lock")

    def _execution_lock_path(self, task_id):
        return self._path(task_id).with_suffix(".execute.lock")

    def load(self, task_id):
        path = self._path(task_id)
        task = read_strict_object(
            path, "orchestration Task", error_type=OrchestrationError)
        if (task.get("task_id") != task_id
                or type(task.get("schema_version")) is not int
                or task["schema_version"] != SCHEMA_VERSION):
            raise OrchestrationError("orchestration record identity/schema mismatch")
        if task.get("request_sha256") != _hash(task.get("request")):
            raise OrchestrationError("orchestration request was changed")
        return task

    def _save(self, task, *, expected=None):
        path = self._path(task["task_id"])
        # Compare-and-save must be one filesystem-serialized transaction.
        # Otherwise independent runners can both observe the same version.
        try:
            with SingleWriterLock(self._mutation_lock_path(task["task_id"])):
                if expected is None and path.exists():
                    raise OrchestrationError("task already exists")
                if expected is not None:
                    current = self.load(task["task_id"])
                    if current["updated_at"] != expected:
                        raise OrchestrationError("concurrent task update")
                task["updated_at"] = _now(expected)
                atomic_write_json(path, task)
                return copy.deepcopy(task)
        except WriterLockError as exc:
            raise OrchestrationError("another writer is updating this task") from exc

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
        task["trace"] = {
            "journal_ref": str(self.state_root / "state" / "logs" /
                               "execution_events" / f"{task_id}.jsonl"),
            "hash_chain": "sha256",
        }
        saved = self._save(task)
        self._emit_event(saved, "task.created", payload={
            "request_sha256": saved["request_sha256"],
            "budget_limit": budget,
            "scope": {key: request.get(key) for key in (
                "region", "delay", "universe", "dataset_ids", "category")
                if key in request},
        })
        return saved

    def _transition(self, task, **changes):
        prior = task["updated_at"]
        task.update(changes)
        saved = self._save(task, expected=prior)
        self._emit_event(saved, "task.transition", payload={
            "status": saved["status"],
            "steps": copy.deepcopy(saved["steps"]),
            "budget": copy.deepcopy(saved["budget"]),
            "failure": ({"step": saved["failure"].get("step"),
                         "reason": saved["failure"].get("reason")}
                        if isinstance(saved.get("failure"), dict) else None),
            "checkpoint_names": sorted(saved.get("checkpoints", {})),
        })
        return saved

    def bind_research_identity(self, task_id, research_id):
        """Durably associate the domain-owned model-budget identity with a task.

        The workflow, not the CLI or orchestrator, must declare its Research
        ID before model invocation. A repeated identical binding is safe, but
        conflicting IDs are never substituted or assigned a fresh budget.
        """
        task = self.load(task_id)
        if (task.get("status") != "RUNNING"
                or task.get("steps", {}).get("execute") != "RUNNING"):
            raise OrchestrationError(
                "research identity can only be bound during execute")
        if not isinstance(research_id, str):
            raise OrchestrationError("domain Research ID is required")
        import re
        prefix = {
            "regular": "regular:campaign:",
            "super": "super:",
            "optimization": "optimization:",
        }[task["workflow"]]
        if re.fullmatch(re.escape(prefix) + r"[0-9a-f]{32}", research_id) is None:
            raise OrchestrationError(
                "domain Research ID does not match workflow identity")
        prior = task.get("research_identity")
        if prior is not None:
            if prior.get("research_id") != research_id:
                raise OrchestrationError(
                    "different domain Research ID already bound to task")
            return task
        saved = self._transition(task, research_identity={
            "research_id": research_id,
            "workflow": task["workflow"],
            "bound_at": _now(),
        })
        self._emit_event(saved, "research.identity_bound", payload={
            "research_id": research_id})
        return saved

    def bind_recovery_target(self, task_id, kind, reference):
        """Persist the exact workflow artifact needed to recover an execute step."""
        task = self.load(task_id)
        if task.get("status") != "RUNNING" or task.get("steps", {}).get("execute") != "RUNNING":
            raise OrchestrationError("recovery target can only be bound during execute")
        expected_kind = "regular_batch" if task["workflow"] == "regular" else "simulation_intent"
        if kind != expected_kind:
            raise OrchestrationError("recovery target kind does not match workflow")
        original = Path(reference).expanduser()
        if original.is_symlink() or original.parent.is_symlink():
            raise OrchestrationError("recovery target cannot be a symlink")
        path = original.resolve()
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
        saved = self._transition(task, recovery_target={
            "kind": kind, "ref": str(path), "bound_at": _now()})
        self._emit_event(saved, "recovery.target_bound", payload={
            "kind": kind, "target_ref": str(path)})
        return saved

    def execute(self, task_id):
        """Fail fast on concurrent executors before touching RUNNING state.

        The lock is held across the injected adapter call. Recovery-target
        callbacks take a separate short-lived mutation lock, not this one.
        """
        try:
            with SingleWriterLock(self._execution_lock_path(task_id)):
                return self._execute_locked(task_id)
        except WriterLockError as exc:
            raise OrchestrationError(
                "another executor is already running this task") from exc

    def _execute_locked(self, task_id):
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
                self._emit_event(task, "task.step_started", step=step, payload={
                    "budget": copy.deepcopy(task["budget"])})
                context = {
                    "task_id": task_id,
                    "bind_research_identity": lambda research_id:
                        self.bind_research_identity(task_id, research_id),
                    "bind_recovery_target": lambda kind, reference:
                        self.bind_recovery_target(task_id, kind, reference),
                    "emit_event": lambda event_type, payload=None:
                        self._emit_event(
                            self.load(task_id), event_type, step=step,
                            payload=payload,
                            research_id=(payload or {}).get("research_id")
                                if isinstance(payload, dict) else None),
                }
                outcome = self.adapter.run(task["workflow"], step,
                                           copy.deepcopy(task["request"]),
                                           copy.deepcopy(task["checkpoints"]),
                                           context=context)
                # Binding the recovery target persists the task while the
                # adapter is running; refresh before the next compare-and-save.
                task = self.load(task_id)
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
                self._emit_event(task, "task.step_completed", step=step, payload={
                    "outcome_sha256": _hash(outcome),
                    "outcome": self._outcome_trace(outcome),
                })
            except Exception as exc:
                task = self.load(task_id)
                # Fail closed: RUNNING execute has unknown external outcome.
                # Never auto-replay it or refund a reserved budget.
                reason = "outcome_unknown_manual_reconciliation_required" if step == "execute" else type(exc).__name__
                task = self._transition(task, status="BLOCKED",
                                        failure={"step": step, "reason": reason})
                self._emit_event(task, "task.step_blocked", step=step, payload={
                    "reason": reason, "exception_type": type(exc).__name__,
                    "budget": copy.deepcopy(task["budget"]),
                })
                return task
        task = self._transition(task, status="COMPLETED",
                                result=copy.deepcopy(task["checkpoints"].get("collect")))
        self._emit_event(task, "task.completed", payload={
            "steps": copy.deepcopy(task["steps"]),
            "budget": copy.deepcopy(task["budget"]),
            "research_id": (task.get("research_identity") or {}).get("research_id"),
        })
        return task

    def resume(self, task_id):
        task = self.load(task_id)
        if task["status"] == "BLOCKED":
            raise OrchestrationError("blocked task requires explicit reconciliation; no automatic replay")
        return self.execute(task_id)

    def reconcile(self, task_id, step, outcome, *, receipt_ref=None, receipt_sha256=None):
        """Reconcile only when no other executor owns this task."""
        try:
            with SingleWriterLock(self._execution_lock_path(task_id)):
                return self._reconcile_locked(
                    task_id, step, outcome,
                    receipt_ref=receipt_ref, receipt_sha256=receipt_sha256)
        except WriterLockError as exc:
            raise OrchestrationError(
                "another executor is already running this task") from exc

    def _reconcile_locked(self, task_id, step, outcome, *,
                          receipt_ref=None, receipt_sha256=None):
        """Accept a checkpoint only when backed by a coordinator-issued durable receipt."""
        task = self.load(task_id)
        if (task["status"] != "BLOCKED" or task.get("failure", {}).get("step") != step
                or task["steps"].get(step) != "RUNNING"):
            raise OrchestrationError("only blocked running step can be reconciled")
        if not isinstance(outcome, dict):
            raise OrchestrationError("reconciled outcome must be a JSON object")
        if not receipt_ref or not receipt_sha256:
            raise OrchestrationError("a verified recovery receipt is required")
        receipt_path = Path(receipt_ref).expanduser().resolve()
        receipt_root = (self.state_root / "state" / "research_v1_1" /
                        "recoveries" / "tasks").resolve()
        if not receipt_path.is_relative_to(receipt_root) or not receipt_path.is_file():
            raise OrchestrationError("recovery receipt must be under the task recovery journal")
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise OrchestrationError("recovery receipt is unreadable") from exc
        actual_sha = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        if actual_sha != receipt_sha256:
            raise OrchestrationError("recovery receipt does not verify this task outcome")
        target = task.get("recovery_target") or {}
        journal_ref = receipt.get("recovery_journal_ref")
        if not isinstance(journal_ref, str) or not journal_ref:
            raise OrchestrationError("domain recovery journal reference is invalid")
        journal_root = (self.state_root / "state" / "research_v1_1" / "recoveries").resolve()
        journal_path = Path(journal_ref).expanduser().resolve()
        if (not journal_path.is_relative_to(journal_root)
                or not journal_path.is_file()):
            raise OrchestrationError("domain recovery journal must stay under the recovery root")
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            journal_sha = hashlib.sha256(journal_path.read_bytes()).hexdigest()
        except (OSError, ValueError) as exc:
            raise OrchestrationError("domain recovery journal is unreadable") from exc
        if (journal.get("status") != "COMPLETED"
                or journal.get("task_id") != task_id
                or journal.get("step") != step
                or journal.get("workflow") != task["workflow"]
                or journal.get("recovery_id") != receipt.get("recovery_id")
                or journal.get("target_ref") != (task.get("recovery_target") or {}).get("ref")
                or journal_sha != receipt.get("recovery_journal_sha256")):
            raise OrchestrationError("domain recovery journal does not verify this task")
        if (receipt.get("status") != "COMPLETED"
                or receipt.get("task_id") != task_id
                or receipt.get("workflow") != task["workflow"]
                or receipt.get("step") != step
                or receipt.get("target_ref") != target.get("ref")
                or receipt.get("target_kind") != target.get("kind")
                or receipt.get("outcome_sha256") != _hash(outcome)):
            raise OrchestrationError("recovery receipt does not verify this task outcome")
        json.dumps(outcome, allow_nan=False)
        task["checkpoints"][step] = copy.deepcopy(outcome)
        task["steps"][step] = "DONE"
        if step == "execute":
            task["budget"]["reserved"] -= 1
            task["budget"]["used"] += 1
        task["reconciliation"] = {
            "receipt_ref": str(receipt_path), "receipt_sha256": actual_sha,
            "reconciled_at": _now(),
        }
        saved = self._transition(task, status="RUNNING", failure=None)
        self._emit_event(saved, "task.reconciled", step=step, payload={
            "receipt_ref": str(receipt_path), "receipt_sha256": actual_sha,
            "outcome_sha256": _hash(outcome),
        })
        return saved


def pool_snapshot(state_root, config):
    """P3: read-only unified pool projection; never promotes a candidate."""
    return CandidatePool(ResearchRecordStore(state_root)).build_index(
        current_policy_sha256=policy_hash(config))
