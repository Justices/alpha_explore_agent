"""Non-authorizing execution-plan evidence for future controlled BRAIN admission.

These records are proposals, NOT approval receipts or credentials.  They pin the
existing orchestration task identity and scope to a durable, inspectable file.
No function in this module can authorize, call, or resume a provider/BRAIN API.
A future separately reviewed approval mechanism must validate and consume
a distinct trusted authorization artifact rather than editing this proposal.
"""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.workflow_contract import (
    command_and_args, validate_workflow_request,
)
from alpha_research.state.fingerprint import fingerprint_json
from alpha_research.state.store import atomic_write_json
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError


SCHEMA_VERSION = 1


def _validated_task(task):
    if not isinstance(task, dict):
        raise OrchestrationError("execution plan requires a saved task record")
    task_id = task.get("task_id")
    if (not isinstance(task_id, str) or len(task_id) != 32
            or any(c not in "0123456789abcdef" for c in task_id)):
        raise OrchestrationError("invalid task ID")
    if task.get("schema_version") != 1:
        raise OrchestrationError("unsupported task schema")
    if task.get("status") != "CREATED":
        raise OrchestrationError("only an unstarted task can be planned")
    request = task.get("request")
    workflow = task.get("workflow")
    validate_workflow_request(workflow, request)
    if fingerprint_json(request) != task.get("request_sha256"):
        raise OrchestrationError("saved request does not match fingerprint")
    command, _ = command_and_args(workflow, request)
    budget = task.get("budget")
    if (not isinstance(budget, dict) or type(budget.get("limit")) is not int
            or budget["limit"] != 1 or budget.get("reserved") != 0
            or budget.get("used") != 0):
        raise OrchestrationError("planning requires unspent one-batch budget")
    if task.get("recovery_target") is not None:
        raise OrchestrationError("planning refuses tasks with recovery state")
    return command


def build_execution_proposal(task):
    """Create inspect-only evidence, never an execution authorization."""
    command = _validated_task(task)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "PROPOSED_NOT_AUTHORIZED",
        "task_id": task["task_id"],
        "workflow": task["workflow"],
        "command": command,
        "request_sha256": task["request_sha256"],
        "request": copy.deepcopy(task["request"]),
        "limits": {"orchestrator_execute_attempts": 1},
        "authorization": None,
        "allow_model_call": False,
        "allow_brain_simulation": False,
        "allow_alpha_submission": False,
    }


def persist_execution_proposal(orchestrator, task_id):
    """Bind a canonical proposal to an immutable CREATED task, no side effects."""
    task = orchestrator.load(task_id)
    proposal = build_execution_proposal(task)
    state_root = orchestrator.state_root
    root = state_root / "state" / "research_v1_1" / "execution_proposals"
    path = root / (task_id + ".json")
    lock_path = root / (task_id + ".lock")
    try:
        with SingleWriterLock(lock_path):
            # Reload under the proposal lock; refuse to create a proposal if
            # the task changed since the caller inspected it.
            current = orchestrator.load(task_id)
            if current != task:
                raise OrchestrationError("task changed while preparing execution proposal")
            if path.exists():
                saved = json.loads(path.read_text(encoding="utf-8"))
                if (not isinstance(saved, dict)
                        or saved.get("proposal_sha256") != fingerprint_json(proposal)
                        or saved.get("proposal") != proposal):
                    raise OrchestrationError("existing execution proposal changed")
                return copy.deepcopy(saved)
            record = {
                "proposal": proposal,
                "proposal_sha256": fingerprint_json(proposal),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            atomic_write_json(path, record)
            return copy.deepcopy(record)
    except WriterLockError as exc:
        raise OrchestrationError("another writer is preparing the task") from exc


def inspect_execution_proposal(orchestrator, task_id):
    """Read-only verification; returned record cannot authorize execution."""
    path = (orchestrator.state_root / "state" / "research_v1_1"
            / "execution_proposals" / (orchestrator._path(task_id).stem + ".json"))
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        task = orchestrator.load(task_id)
        expected = build_execution_proposal(task)
        if (not isinstance(record, dict) or record.get("proposal") != expected
                or record.get("proposal_sha256") != fingerprint_json(expected)):
            raise OrchestrationError("execution proposal identity or fingerprint mismatch")
        return record
    except (OSError, ValueError, TypeError) as exc:
        raise OrchestrationError("execution proposal is missing or invalid") from exc
