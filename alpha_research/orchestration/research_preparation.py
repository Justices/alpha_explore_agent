"""P4.15 staged research preparation in a strictly isolated offline mock root.

This coordinator is a *pre-execution* state machine. It exercises the real
Super/Optimization candidate construction and validation logic with scripted
Gateway and Model fakes, then persists the internal Manifest. It intentionally
does not run MainOrchestrator.execute: a model-preparation test must not consume
the Task's one-shot Simulation budget or create a POST recovery target.

No live mode exists here. A permanently marked mock workspace is mandatory,
and a PREPARING/BLOCKED journal cannot be replayed by this entrypoint.
"""
import json
from copy import deepcopy
from pathlib import Path

from alpha_research.config import DEFAULT_CONFIG
from alpha_research.orchestration.domain_mock import (
    DomainMockGateway, DomainMockModel, mock_domain_boundary,
)
from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.manifest_builder import (
    inspect_generated_manifest, make_agent_preparation_handoff,
    read_generated_manifest,
)
from alpha_research.orchestration.mock_workspace import is_mock_workspace
from alpha_research.state.store import atomic_write_json
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError

_VERSION = 1
_ROOT_FOLDER = "research_preparation"
_TERMINAL = "RESEARCH_PREPARED_OFFLINE"
_ALLOWED = {"super", "optimization"}


def _journal_path(orchestrator, task_id):
    orchestrator._path(task_id)
    return (Path(orchestrator.state_root) / "state" / "research_v1_1" /
            _ROOT_FOLDER / (task_id + ".json"))


def _assert_mock_root(orchestrator):
    stop = Path(orchestrator.state_root) / "state" / "stop.flag"
    if stop.exists() or stop.is_symlink():
        raise OrchestrationError("research preparation blocked by stop signal")
    if not is_mock_workspace(orchestrator.state_root):
        raise OrchestrationError(
            "research preparation mock requires a separately marked offline mock root")


def _assert_fresh(task):
    if (task.get("status") != "CREATED"
            or task.get("workflow") not in _ALLOWED
            or task.get("budget") != {"limit": 1, "used": 0, "reserved": 0}
            or task.get("checkpoints") != {}
            or task.get("research_identity") is not None
            or task.get("recovery_target") is not None):
        raise OrchestrationError(
            "research preparation requires a fresh unexecuted Super/Optimization task")
    req = task["request"]
    if (not isinstance(req.get("region"), str)
            or not req["region"].strip()
            or req.get("delay") != 1
            or not isinstance(req.get("universe"), str)
            or req["universe"] != "TOP3000"):
        raise OrchestrationError(
            "offline research mock supports explicit Delay 1 / TOP3000 scope only")
    if task["workflow"] == "optimization" and not req.get("alpha_id"):
        raise OrchestrationError("optimization source Alpha ID required")


def _safe_journal(path):
    if path.parent.is_symlink() or path.is_symlink():
        raise OrchestrationError("research preparation journal is symlinked")
    if path.parent.exists() and not path.parent.is_dir():
        raise OrchestrationError("research preparation journal parent unsafe")


def _load_journal(path, task_id):
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OrchestrationError(
            "research preparation journal unreadable; manual reconciliation required"
        ) from exc
    if (not isinstance(report, dict)
            or report.get("schema_version") != _VERSION
            or report.get("task_id") != task_id
            or report.get("mode") != "offline_domain_mock"
            or report.get("workflow") not in _ALLOWED
            or report.get("request_sha256") is None
            or report.get("status") not in {
                "PREPARING", "BLOCKED_MANUAL_REVIEW", _TERMINAL,
            }):
        raise OrchestrationError(
            "research preparation journal identity/status drift; manual review required")
    return report


def inspect_research_preparation(orchestrator, task_id):
    """Read-only state inspection, never displays Alpha expressions."""
    task = orchestrator.load(task_id)
    path = _journal_path(orchestrator, task_id)
    _safe_journal(path)
    if not path.exists():
        return {
            "schema_version": _VERSION, "task_id": task_id,
            "status": "AWAITING_RESEARCH_PREPARATION",
            "mode": "offline_domain_mock",
            "task_status": task["status"],
            "permission_to_dispatch": False, "model_call_authorized": False,
            "brain_simulation_authorized": False,
            "alpha_submission_authorized": False,
            "automatic_retry_allowed": False,
            "brain_invoked": False, "provider_invoked": False,
        }
    report = _load_journal(path, task_id)
    if (report["workflow"] != task["workflow"]
            or report["request_sha256"] != task["request_sha256"]):
        raise OrchestrationError(
            "research preparation task identity drift; manual review required")
    if report["status"] == _TERMINAL:
        snapshot = inspect_generated_manifest(orchestrator, task_id)
        if (snapshot["status"] != "GENERATED_LOCAL_NOT_AUTHORIZED"
                or report.get("manifest_sha256") != snapshot.get("manifest_sha256")):
            raise OrchestrationError(
                "research preparation snapshot mismatch; manual reconciliation required")
    return {
        **report,
        "permission_to_dispatch": False,
        "model_call_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "automatic_retry_allowed": False,
        "brain_invoked": False, "provider_invoked": False,
    }


def prepare_offline_research(orchestrator, task_id):
    """Single-use preparation entrypoint; synthetic Agent, no API calls or POST.

    Repeating a successful call returns a reconciled fingerprint-only report.
    An interrupted/failed preparation never generates a second candidate.
    """
    _assert_mock_root(orchestrator)
    task = orchestrator.load(task_id)
    _assert_fresh(task)
    path = _journal_path(orchestrator, task_id)
    _safe_journal(path)
    try:
        with SingleWriterLock(path.with_suffix(".lock")):
            _assert_mock_root(orchestrator)
            task = orchestrator.load(task_id)
            _assert_fresh(task)
            _safe_journal(path)
            if path.exists():
                existing = _load_journal(path, task_id)
                if existing["status"] != _TERMINAL:
                    raise OrchestrationError(
                        "research preparation was interrupted/blocked; no automatic retry")
                return inspect_research_preparation(orchestrator, task_id)
            if read_generated_manifest(orchestrator, task_id) is not None:
                raise OrchestrationError(
                    "Agent Manifest already exists without preparation journal")
            journal = {
                "schema_version": _VERSION,
                "task_id": task_id,
                "workflow": task["workflow"],
                "request_sha256": task["request_sha256"],
                "mode": "offline_domain_mock",
                "status": "PREPARING",
                "manifest_sha256": None,
                "failure_type": None,
            }
            atomic_write_json(path, journal)
            request = task["request"]
            config = deepcopy(DEFAULT_CONFIG)
            config["scope"]["region"] = request["region"]
            config["scope"]["delay"] = request["delay"]
            config["scope"]["universe"] = request["universe"]
            gateway = DomainMockGateway(request["region"], request.get("alpha_id"))
            model = DomainMockModel()
            handoff = make_agent_preparation_handoff(orchestrator, task_id)
            try:
                # Every remote/model entrypoint is swapped out inside this scope.
                with mock_domain_boundary(request["region"]):
                    if task["workflow"] == "super":
                        from alpha_research.workflows.super_alpha.workflow import (
                            run_super_research,
                        )
                        result = run_super_research(
                            gateway, model, config, orchestrator.state_root,
                            region=request["region"], delay=request["delay"],
                            objective=request["objective"],
                            preparation_handoff=handoff,
                        )
                    else:
                        from alpha_research.workflows.optimization.workflow import (
                            run_optimization,
                        )
                        result = run_optimization(
                            gateway, model, config, orchestrator.state_root,
                            region=request["region"],
                            alpha_id=request["alpha_id"],
                            objective=request["objective"],
                            preparation_handoff=handoff,
                        )
                snapshot = inspect_generated_manifest(orchestrator, task_id)
                if (result.get("status") != "AWAITING_INDEPENDENT_AUTHORIZATION"
                        or snapshot["status"] != "GENERATED_LOCAL_NOT_AUTHORIZED"
                        or result.get("simulation_invoked") is not False
                        or "simulate_super" in gateway.calls
                        or "simulate_batch" in gateway.calls
                        or "submit_alpha" in gateway.calls):
                    raise OrchestrationError(
                        "research preparation unexpectedly crossed execution boundary")
                finished = orchestrator.load(task_id)
                _assert_fresh(finished)
                journal.update(
                    status=_TERMINAL,
                    manifest_sha256=snapshot["manifest_sha256"],
                )
                atomic_write_json(path, journal)
            except Exception as exc:
                # No replay: model invocation status could become ambiguous if
                # this is ever generalized beyond deterministic mock clients.
                journal.update(
                    status="BLOCKED_MANUAL_REVIEW",
                    failure_type=type(exc).__name__,
                )
                atomic_write_json(path, journal)
                raise
            return inspect_research_preparation(orchestrator, task_id)
    except WriterLockError as exc:
        raise OrchestrationError(
            "another research preparation is active; automatic retry forbidden") from exc
