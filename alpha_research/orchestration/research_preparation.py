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
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.manifest_builder import (
    inspect_generated_manifest, make_agent_preparation_handoff,
    read_generated_manifest,
)
from alpha_research.orchestration.mock_workspace import is_mock_workspace
from alpha_research.orchestration.research_task_contract import is_fresh_research_task
from alpha_research.orchestration.research_runtime_adapter import (
    OfflineResearchRuntimeAdapter, inspect_runtime_attempt,
)
from alpha_research.orchestration.runtime_config import OFFLINE_PREPARATION_POLICY
from alpha_research.state.sealed import is_sealed_state, write_sealed_json
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError

_VERSION = 1
_ROOT_FOLDER = "research_preparation"
_TERMINAL = "RESEARCH_PREPARED_OFFLINE"
_ALLOWED = {"super", "optimization"}
_JOURNAL_FIELDS = frozenset({
    "schema_version", "task_id", "workflow", "request_sha256", "mode",
    "status", "manifest_sha256", "failure_type", "model_response_sha256",
    "catalog_reads", "state_sha256",
})


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
    if not is_fresh_research_task(task):
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
        _safe_journal(path)
        # A hostile or accidentally corrupted journal must not allocate
        # unbounded memory merely because an operator inspected the Task.
        if not path.is_file() or not 0 < path.stat().st_size <= 65_536:
            raise ValueError("research journal absent or exceeds size limit")
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OrchestrationError(
            "research preparation journal unreadable; manual reconciliation required"
        ) from exc
    if (not isinstance(report, dict)
            or set(report) != _JOURNAL_FIELDS
            or not is_sealed_state(report)
            or type(report["schema_version"]) is not int
            or report["schema_version"] != _VERSION
            or report["task_id"] != task_id
            or report["mode"] != "offline_domain_mock"
            or report["workflow"] not in _ALLOWED
            or not isinstance(report["request_sha256"], str)
            or len(report["request_sha256"]) != 64
            or report["status"] not in {
                "PREPARING", "BLOCKED_MANUAL_REVIEW", _TERMINAL,
            }
            or not isinstance(report["catalog_reads"], list)
            or any(not isinstance(v, str) for v in report["catalog_reads"])
            or (report["failure_type"] is not None
                and not isinstance(report["failure_type"], str))):
        raise OrchestrationError(
            "research preparation journal identity/integrity drift; manual review required")
    if report["status"] == _TERMINAL:
        if (not isinstance(report["manifest_sha256"], str)
                or len(report["manifest_sha256"]) != 64
                or not isinstance(report["model_response_sha256"], str)
                or len(report["model_response_sha256"]) != 64
                or not report["catalog_reads"]
                or report["failure_type"] is not None):
            raise OrchestrationError(
                "research preparation terminal journal incomplete; manual review required")
    elif (report["manifest_sha256"] is not None
          or report["model_response_sha256"] is not None
          or report["catalog_reads"]):
        raise OrchestrationError(
            "unfinished research preparation contains terminal evidence")
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
        if "model_response_sha256" in report:
            attempt = inspect_runtime_attempt(orchestrator, task_id)
            if (attempt["status"] != "MODEL_MOCK_COMPLETED"
                    or attempt["response_sha256"] != report["model_response_sha256"]):
                raise OrchestrationError(
                    "research model attempt evidence drift; reconciliation required")
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
                "model_response_sha256": None,
                "catalog_reads": [],
            }
            write_sealed_json(path, journal)
            request = task["request"]
            config = deepcopy(DEFAULT_CONFIG)
            config["scope"]["region"] = request["region"]
            config["scope"]["delay"] = request["delay"]
            config["scope"]["universe"] = request["universe"]
            gateway = DomainMockGateway(request["region"], request.get("alpha_id"))
            model = DomainMockModel()
            handoff = make_agent_preparation_handoff(orchestrator, task_id)
            try:
                runtime = OfflineResearchRuntimeAdapter(
                    orchestrator, task_id, gateway, model,
                    OFFLINE_PREPARATION_POLICY)
                # Production ports are NEVER instantiated. A mock-specific
                # scoped Catalog client and a write-ahead scripted model
                # reservation are active only during this context.
                with runtime, mock_domain_boundary(request["region"]):
                    if task["workflow"] == "super":
                        from alpha_research.workflows.super_alpha.workflow import (
                            run_super_research,
                        )
                        result = run_super_research(
                            runtime.gateway, model, config, orchestrator.state_root,
                            region=request["region"], delay=request["delay"],
                            objective=request["objective"],
                            preparation_handoff=handoff,
                        )
                    else:
                        from alpha_research.workflows.optimization.workflow import (
                            run_optimization,
                        )
                        result = run_optimization(
                            runtime.gateway, model, config, orchestrator.state_root,
                            region=request["region"],
                            alpha_id=request["alpha_id"],
                            objective=request["objective"],
                            preparation_handoff=handoff,
                        )
                runtime_report = runtime.verify_completed()
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
                    model_response_sha256=runtime_report["response_sha256"],
                    catalog_reads=runtime_report["catalog_reads"],
                )
                write_sealed_json(path, journal)
            except Exception as exc:
                # No replay: model invocation status could become ambiguous if
                # this is ever generalized beyond deterministic mock clients.
                journal.update(
                    status="BLOCKED_MANUAL_REVIEW",
                    failure_type=type(exc).__name__,
                    manifest_sha256=None,
                    model_response_sha256=None,
                    catalog_reads=[],
                )
                write_sealed_json(path, journal)
                raise
            return inspect_research_preparation(orchestrator, task_id)
    except WriterLockError as exc:
        raise OrchestrationError(
            "another research preparation is active; automatic retry forbidden") from exc
