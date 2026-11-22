"""P4.18 research runtime adapter: *scripted offline transports only*.

A production driver or paid model cannot be supplied to this adapter. It
runs the ACTUAL Super/Optimization preparation workflow with synthetic data,
but passes every catalog read through the P4.17 scoped request contract and
journals the model attempt BEFORE the scripted response. On a lost outcome,
do not replay. This is a rehearsal for a future externally admitted runtime,
NOT its live implementation or an operator authorization.
"""
import json
import re
from contextvars import ContextVar
from pathlib import Path

from alpha_research.agents.governance import validate_policy
from alpha_research.orchestration.domain_mock import (
    DomainMockGateway, DomainMockModel,
)
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.mock_workspace import is_mock_workspace
from alpha_research.orchestration.research_task_contract import is_fresh_research_task
from alpha_research.orchestration.research_catalog_protocol import (
    research_catalog_request_allowed,
)
from alpha_research.state.fingerprint import fingerprint_json
from alpha_research.state.sealed import is_sealed_state, write_sealed_json
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError

_RUNTIME = ContextVar("offline_research_runtime", default=None)
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_FINAL = "MODEL_MOCK_COMPLETED"
_TASK_MODEL_ACTION = {"super": "construct_super", "optimization": "optimize_alpha"}


def active_offline_runtime():
    return _RUNTIME.get()


def _attempt_path(orchestrator, task_id):
    orchestrator._path(task_id)
    return (Path(orchestrator.state_root) / "state" / "research_v1_1" /
            "research_runtime_attempts" / (task_id + ".json"))


def _check_path(path):
    if (path.is_symlink() or path.parent.is_symlink()
            or (path.parent.exists() and not path.parent.is_dir())):
        raise OrchestrationError("offline research runtime attempt path unsafe")


def inspect_runtime_attempt(orchestrator, task_id):
    """Strict read-only attempt recovery check; no model or BRAIN imports."""
    task = orchestrator.load(task_id)
    path = _attempt_path(orchestrator, task_id)
    base = {
        "schema_version": 1, "task_id": task_id,
        "mode": "offline_scripted_only",
        "model_call_authorized": False,
        "brain_read_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "permission_to_dispatch": False,
        "automatic_retry_allowed": False,
        "provider_invoked": False, "brain_invoked": False,
    }
    try:
        _check_path(path)
        if not path.exists():
            return {**base, "status": "NO_MODEL_ATTEMPT"}
        raw = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(raw, dict) or not is_sealed_state(raw)
                or set(raw) != {
                    "schema_version", "task_id", "request_sha256",
                    "mode", "status", "call_id", "task_name",
                    "max_model_calls", "max_reported_tokens",
                    "policy_sha256", "response_sha256", "state_sha256",
                }
                or type(raw["schema_version"]) is not int
                or raw["schema_version"] != 1
                or raw["task_id"] != task_id
                or raw["request_sha256"] != task["request_sha256"]
                or raw["mode"] != "offline_scripted_only"
                or raw["status"] not in {"MODEL_CALL_MAY_HAVE_STARTED",
                                          "MODEL_MOCK_UNKNOWN", _FINAL}
                or not isinstance(raw["call_id"], str)
                or _HEX32.fullmatch(raw["call_id"]) is None
                or raw["task_name"] != _TASK_MODEL_ACTION.get(task["workflow"])
                or type(raw["max_model_calls"]) is not int
                or not 1 <= raw["max_model_calls"] <= 10_000_000
                or type(raw["max_reported_tokens"]) is not int
                or not 1 <= raw["max_reported_tokens"] <= 10_000_000
                or not isinstance(raw["policy_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", raw["policy_sha256"])
                or not ((raw["status"] == _FINAL
                         and isinstance(raw["response_sha256"], str)
                         and re.fullmatch(r"[0-9a-f]{64}",
                                          raw["response_sha256"]))
                        or (raw["status"] != _FINAL
                            and raw["response_sha256"] is None))):
            raise OrchestrationError("runtime model attempt integrity invalid")
    except (OSError, ValueError, TypeError, OrchestrationError):
        return {**base, "status": "RECONCILIATION_REQUIRED"}
    return {
        **base,
        "status": raw["status"],
        "call_id": raw["call_id"],
        "task_name": raw["task_name"],
        "policy_sha256": raw["policy_sha256"],
        "response_sha256": raw["response_sha256"],
        "attempt_outcome_known": raw["status"] == _FINAL,
    }


class _ScopedScriptedCatalog:
    """Client boundary: exact wire validation before an in-process fake call."""
    def __init__(self, gateway, task, scope):
        if type(gateway) is not DomainMockGateway:
            raise OrchestrationError(
                "offline research catalog refuses real Gateway/driver transports")
        self._gateway = gateway
        self._scope = scope
        self._source_alpha = (task["request"].get("alpha_id")
                              if task["workflow"] == "optimization" else None)
        self._task_id = task["task_id"]
        self.calls = []
        self.local_case_id = None

    def call(self, command, **params):
        # set_case is LOCAL bookkeeping, not a BRAIN Catalog operation.
        # Never forward it to the transport; do not add it to the wire policy.
        if command == "set_case":
            case, run_id = params.get("case_key"), params.get("run_id")
            if (set(params) != {"case_key", "run_id"}
                    or not isinstance(case, str)
                    or _HEX32.fullmatch(case) is None
                    or case != run_id or self.local_case_id is not None):
                raise OrchestrationError("offline local case identity invalid")
            self.local_case_id = case
            return {}
        wire = dict(params)
        if command in {"get_operators", "get_datasets", "get_datafields",
                       "readonly_probe_datasets"}:
            wire = {**self._scope, **wire}
        if not research_catalog_request_allowed(
                command, wire, self._scope,
                source_alpha_id=self._source_alpha):
            raise OrchestrationError(
                "offline research Catalog action or pinned scope not admitted")
        # Exercise the independently exposed driver-frame entrypoint, not a
        # second copy of the client predicate. A driver rejection MUST stop
        # before any scripted transport operation.
        from alpha_research.integrations.brain.driver import (
            research_catalog_frame_allowed,
        )
        if not research_catalog_frame_allowed(
                {"cmd": command, **wire}, self._scope,
                source_alpha_id=self._source_alpha):
            raise OrchestrationError("scripted driver rejected research Catalog read")
        result = self._gateway.call(command, **params)
        self.calls.append(command)
        return result


class OfflineResearchRuntimeAdapter:
    """One Task, one scripted model decision, no paid provider capability."""

    def __init__(self, orchestrator, task_id, gateway, model, governance_policy):
        if not is_mock_workspace(orchestrator.state_root):
            raise OrchestrationError(
                "offline research runtime requires a claimed mock workspace")
        if type(model) is not DomainMockModel:
            raise OrchestrationError("offline runtime refuses a real model client")
        task = orchestrator.load(task_id)
        if not is_fresh_research_task(task):
            raise OrchestrationError(
                "offline research runtime requires an unexecuted scoped Task")
        policy = validate_policy(governance_policy)
        scope = {
            "region": task["request"]["region"],
            "delay": task["request"]["delay"],
            "universe": task["request"]["universe"],
            "instrument_type": "EQUITY",
        }
        if not research_catalog_request_allowed(
                "get_datasets", scope, scope):
            raise OrchestrationError("offline runtime Task scope unpinned")
        self.orchestrator = orchestrator
        self.task_id = task_id
        self.workflow = task["workflow"]
        self.request_sha256 = task["request_sha256"]
        self.model = model
        self.policy = policy
        self.gateway = _ScopedScriptedCatalog(gateway, task, scope)
        self._attempt = _attempt_path(orchestrator, task_id)
        _check_path(self._attempt)
        if self._attempt.exists():
            raise OrchestrationError(
                "research model attempt already exists; inspect before replay")
        self._token = None
        self._model_calls = 0

    def __enter__(self):
        if _RUNTIME.get() is not None:
            raise OrchestrationError("nested offline runtime forbidden")
        self._token = _RUNTIME.set(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._token is not None:
            _RUNTIME.reset(self._token)
            self._token = None
        return False

    def reserve_scripted_model_call(self, task_name, context):
        if (task_name != _TASK_MODEL_ACTION[self.workflow]
                or not isinstance(context, dict)
                or self._model_calls != 0
                or self.policy["max_calls"] < 1):
            raise OrchestrationError("offline model call budget or task exceeded")
        _check_path(self._attempt)
        lock_path = self._attempt.with_suffix(".lock")
        if lock_path.is_symlink():
            raise OrchestrationError("research runtime writer lock is unsafe")
        try:
            with SingleWriterLock(lock_path):
                _check_path(self._attempt)
                if self._attempt.exists() or self._attempt.is_symlink():
                    raise OrchestrationError(
                        "research model outcome requires reconciliation")
                if self._model_calls:
                    raise OrchestrationError("research model call already reserved")
                task = self.orchestrator.load(self.task_id)
                if (not _fresh_task(task)
                        or task["workflow"] != self.workflow
                        or task["request_sha256"] != self.request_sha256):
                    raise OrchestrationError(
                        "offline research Task drift before model reservation")
                import uuid
                row = {
                    "schema_version": 1,
                    "task_id": self.task_id,
                    "request_sha256": self.request_sha256,
                    "mode": "offline_scripted_only",
                    "status": "MODEL_CALL_MAY_HAVE_STARTED",
                    "call_id": uuid.uuid4().hex,
                    "task_name": task_name,
                    "max_model_calls": self.policy["max_calls"],
                    "max_reported_tokens": self.policy["max_reported_tokens"],
                    "policy_sha256": fingerprint_json(self.policy),
                    "response_sha256": None,
                }
                write_sealed_json(self._attempt, row)
                self._model_calls = 1
        except WriterLockError as exc:
            raise OrchestrationError(
                "research model reservation busy; no automatic retry") from exc

    def conclude_scripted_model_call(self, response=None, *, unknown=False):
        """Single-writer terminal transition; a later callback cannot overwrite it.

        Reserve and conclude share the same OS lock. An interrupted or
        ambiguous call remains unreplayable, even from another runtime.
        """
        _check_path(self._attempt)
        if self._model_calls != 1:
            raise OrchestrationError("offline model completion without reservation")
        lock_path = self._attempt.with_suffix(".lock")
        if lock_path.is_symlink():
            raise OrchestrationError("research runtime writer lock is unsafe")
        try:
            with SingleWriterLock(lock_path):
                _check_path(self._attempt)
                snapshot = inspect_runtime_attempt(
                    self.orchestrator, self.task_id)
                if (snapshot["status"] != "MODEL_CALL_MAY_HAVE_STARTED"
                        or snapshot.get("policy_sha256") != fingerprint_json(self.policy)):
                    raise OrchestrationError(
                        "offline model reservation changed; no terminal replay")
                # Full schema, seal, scope, task and policy were validated
                # while holding the lock. Recheck that no other writer
                # changed this exact reservation before transition.
                raw = json.loads(self._attempt.read_text(encoding="utf-8"))
                if (not is_sealed_state(raw)
                        or raw["status"] != "MODEL_CALL_MAY_HAVE_STARTED"
                        or raw["task_id"] != self.task_id
                        or raw["call_id"] != snapshot["call_id"]
                        or raw["policy_sha256"] != snapshot["policy_sha256"]):
                    raise OrchestrationError("offline model reservation changed")
                if unknown:
                    raw["status"] = "MODEL_MOCK_UNKNOWN"
                else:
                    if not isinstance(response, dict):
                        raise OrchestrationError(
                            "offline model response must be a JSON object")
                    raw["response_sha256"] = fingerprint_json(response)
                    raw["status"] = _FINAL
                write_sealed_json(self._attempt, raw)
        except WriterLockError as exc:
            raise OrchestrationError(
                "research model completion busy; no automatic retry") from exc

    def verify_completed(self):
        result = inspect_runtime_attempt(self.orchestrator, self.task_id)
        if (result["status"] != _FINAL or self._model_calls != 1
                or result.get("policy_sha256") != fingerprint_json(self.policy)
                or self.gateway.local_case_id is None):
            raise OrchestrationError(
                "offline research runtime attempt incomplete; no retry permitted")
        return {
            **result,
            "catalog_reads": list(self.gateway.calls),
            "local_case_only": True,
        }
