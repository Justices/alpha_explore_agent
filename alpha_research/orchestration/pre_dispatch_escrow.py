"""P4.7.3 durable pre-dispatch escrow. Strictly NON-EXECUTABLE.

The escrow snapshots a verified, immutable Simulation Intent, staged scope,
operator consent AUDIT reservation, task budget and ModelCallLedger BEFORE
any possible future external POST. It is NOT an authorization, nor does it
transition the Intent to POSTING, open a Gateway, call a model or issue a token.

The consumer that could perform a real POST does not exist. A future trusted
issuer, atomically consumed live grant, and independent Gateway/driver gates
must be reviewed and implemented separately.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from alpha_research.agents.governance import ModelBudgetError, ModelCallLedger, validate_policy
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.mock_workspace import marker_path
from alpha_research.orchestration.operator_consent import inspect_consent_reservation
from alpha_research.orchestration.simulation_attempt_admission import (
    inspect_staged_simulation_attempt, _paths as _staging_paths,
)
from alpha_research.state.fingerprint import fingerprint_json
from alpha_research.state.store import atomic_write_json
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError

_SCHEMA = 1
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_PREPARED = "PREDISPATCH_ESCROW_NOT_EXECUTABLE"
_RECONCILE = "RECONCILIATION_REQUIRED"


def _root(orchestrator):
    return (Path(orchestrator.state_root) / "state" / "research_v1_1"
            / "simulation_predispatch_escrow")


def _paths(orchestrator, task_id):
    orchestrator._path(task_id)
    root = _root(orchestrator)
    return root, root / "escrow_index.json", root / (task_id + ".json")


def _assert_offline_environment(orchestrator):
    root = Path(orchestrator.state_root)
    escrow_root = _root(orchestrator)
    if (escrow_root.is_symlink()
            or (escrow_root.exists() and not escrow_root.is_dir())):
        raise OrchestrationError("pre-dispatch escrow directory unsafe")
    stop = root / "state" / "stop.flag"
    marker = marker_path(root)
    if stop.exists() or stop.is_symlink():
        raise OrchestrationError("global stop signal blocks pre-dispatch escrow")
    if marker.exists() or marker.is_symlink():
        raise OrchestrationError("mock workspace blocks pre-dispatch escrow")


def _read_json_file(path, label):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise OrchestrationError(f"{label} missing or symlinked")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OrchestrationError(f"{label} unreadable") from exc
    if not isinstance(data, dict):
        raise OrchestrationError(f"{label} invalid")
    return data


def _index(root):
    """Canonical first-write index: globally one escrow per task/grant/attempt."""
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise OrchestrationError("pre-dispatch escrow directory unsafe")
    index_path = root / "escrow_index.json"
    if index_path.is_symlink():
        raise OrchestrationError("pre-dispatch escrow index symlinked")
    projections = list(root.glob("[0-9a-f]" * 32 + ".json"))
    if not index_path.exists():
        if projections:
            raise OrchestrationError("pre-dispatch escrow index missing with projection")
        return {"schema_version": _SCHEMA, "by_attempt": {}, "by_task": {},
                "by_grant": {}}
    body = _read_json_file(index_path, "pre-dispatch escrow index")
    if (set(body) != {"schema_version", "by_attempt", "by_task", "by_grant"}
            or type(body["schema_version"]) is not int
            or body["schema_version"] != _SCHEMA
            or not isinstance(body["by_attempt"], dict)
            or not isinstance(body["by_task"], dict)
            or not isinstance(body["by_grant"], dict)
            or len(body["by_attempt"]) != len(body["by_task"])
            or len(body["by_attempt"]) != len(body["by_grant"])):
        raise OrchestrationError("pre-dispatch escrow index schema invalid")
    for aid, entry in body["by_attempt"].items():
        if (not isinstance(aid, str) or _HEX64.fullmatch(aid) is None
                or not isinstance(entry, dict)
                or set(entry) != {"task_id", "grant_id", "record_sha256"}
                or not isinstance(entry["task_id"], str)
                or _HEX32.fullmatch(entry["task_id"]) is None
                or not isinstance(entry["grant_id"], str)
                or _HEX32.fullmatch(entry["grant_id"]) is None
                or not isinstance(entry["record_sha256"], str)
                or _HEX64.fullmatch(entry["record_sha256"]) is None
                or body["by_task"].get(entry["task_id"]) != aid
                or body["by_grant"].get(entry["grant_id"]) != aid):
            raise OrchestrationError("pre-dispatch escrow reciprocal mapping invalid")
        record = _read_json_file(
            root / (entry["task_id"] + ".json"), "pre-dispatch escrow projection")
        if (record.get("schema_version") != _SCHEMA
                or record.get("task_id") != entry["task_id"]
                or record.get("grant_id") != entry["grant_id"]
                or record.get("attempt_id") != aid
                or record.get("status") != _PREPARED
                or record.get("permission_to_dispatch") is not False
                or fingerprint_json(record) != entry["record_sha256"]):
            raise OrchestrationError("pre-dispatch escrow projection drift")
    for mapping, field in ((body["by_task"], "task_id"),
                           (body["by_grant"], "grant_id")):
        for value, aid in mapping.items():
            match = _HEX32.fullmatch(value) if isinstance(value, str) else None
            if (match is None or not isinstance(aid, str)
                    or aid not in body["by_attempt"]
                    or body["by_attempt"][aid][field] != value):
                raise OrchestrationError("pre-dispatch escrow reverse mapping invalid")
    if any(p.is_symlink() or p.stem not in body["by_task"] for p in projections):
        raise OrchestrationError("pre-dispatch escrow orphan projection")
    return body


def _evidence(orchestrator, task_id, governance_policy):
    """Inspect immutable evidence plus the actual model ledger; no writes."""
    _assert_offline_environment(orchestrator)
    task = orchestrator.load(task_id)
    if (task["status"] != "CREATED"
            or task["workflow"] not in {"super", "optimization"}
            or task["budget"] != {"limit": 1, "reserved": 0, "used": 0}
            or task.get("recovery_target") is not None
            or task.get("research_identity") is not None
            or any(state != "PENDING" for state in task["steps"].values())):
        raise OrchestrationError("pre-dispatch escrow requires fresh unexecuted task")
    staged = inspect_staged_simulation_attempt(orchestrator, task_id)
    if staged["status"] != "STAGED_NO_DISPATCH":
        raise OrchestrationError("pre-dispatch escrow staging needs reconciliation")
    consent = inspect_consent_reservation(orchestrator, task_id)
    if consent["status"] != "RESERVED_CONSENT_AUDIT_ONLY":
        raise OrchestrationError("pre-dispatch escrow consent audit is not reserved")
    stage_path, intent_path, _ = _staging_paths(orchestrator, task_id)
    consent_root = (Path(orchestrator.state_root) / "state" / "research_v1_1"
                    / "operator_consent_audit")
    stage = _read_json_file(stage_path, "pre-dispatch staging journal")
    intent = _read_json_file(intent_path, "pre-dispatch Simulation Intent")
    consent_record = _read_json_file(
        consent_root / (task_id + ".json"), "pre-dispatch consent audit")
    if (consent_record.get("intent_sha256") != fingerprint_json(intent)
            or consent_record.get("grant_id") != consent["grant_id"]
            or stage.get("authorization_id") != staged["authorization_id"]
            or stage.get("research_id") != staged["research_id"]
            or intent.get("status") != "PREPARED"
            or intent.get("metadata", {}).get("dispatch_authorized") is not False
            or intent.get("metadata", {}).get("task_id") != task_id
            or intent.get("metadata", {}).get("authorization_id") != staged["authorization_id"]):
        raise OrchestrationError("pre-dispatch escrow evidence identity drift")
    research_id = staged["research_id"]
    policy = validate_policy(governance_policy)
    ledger = ModelCallLedger(orchestrator.state_root, policy)
    budget_path, _ = ledger._paths(research_id)
    if budget_path.is_symlink() or budget_path.parent.is_symlink():
        raise OrchestrationError("pre-dispatch escrow model ledger symlinked")
    try:
        budget_record = ledger._load(budget_path, research_id)
        budget = ledger._summarize(budget_record)
    except (ModelBudgetError, OSError, ValueError, TypeError, KeyError) as exc:
        raise OrchestrationError("pre-dispatch escrow model budget unreadable") from exc
    # The consent receipt allows ZERO further model calls. A prior attempt,
    # even successful, means the frozen no-model plan is no longer fresh.
    if (budget["attempts"] != 0 or budget["unknown"] or budget["unresolved"]
            or budget["unknown_usage_attempts"] or budget["reported_tokens"]):
        raise OrchestrationError("pre-dispatch escrow model budget not fresh")
    return {
        "task_id": task_id,
        "grant_id": consent["grant_id"],
        "scope_authorization_id": staged["authorization_id"],
        "operator_approval_id": consent_record["operator_approval_id"],
        "research_id": research_id,
        "run_id": intent["run_id"],
        "batch_id": intent["batch_id"],
        "workflow": task["workflow"],
        "simulation_sha256": staged["simulation_sha256"],
        "intent_sha256": fingerprint_json(intent),
        "consent_sha256": fingerprint_json(consent_record),
        "staging_sha256": fingerprint_json(stage),
        "governance_policy_sha256": fingerprint_json(policy),
        "model_budget_attempts": budget["attempts"],
    }


def inspect_pre_dispatch_escrow(orchestrator, task_id, governance_policy=None):
    """Read-only status; never produces an executable capability or repairs."""
    root, _, projection = _paths(orchestrator, task_id)
    try:
        index = _index(root)
        attempt_id = index["by_task"].get(task_id)
        if attempt_id is None:
            if projection.is_symlink() or projection.exists():
                raise OrchestrationError("pre-dispatch escrow orphan task")
            return {"task_id": task_id, "status": "NOT_ESCROWED",
                    "permission_to_dispatch": False,
                    "automatic_retry_allowed": False}
        record = _read_json_file(projection, "pre-dispatch escrow projection")
        if governance_policy is not None:
            evidence = _evidence(orchestrator, task_id, governance_policy)
            for name, value in evidence.items():
                if record.get(name) != value:
                    raise OrchestrationError("pre-dispatch escrow evidence drift")
        return {"task_id": task_id, "attempt_id": attempt_id,
                "status": _PREPARED, "permission_to_dispatch": False,
                "automatic_retry_allowed": False}
    except (OrchestrationError, OSError, ValueError, TypeError,
            KeyError, AttributeError):
        return {"task_id": task_id, "status": _RECONCILE,
                "permission_to_dispatch": False,
                "automatic_retry_allowed": False}


def reserve_pre_dispatch_escrow(orchestrator, task_id, governance_policy):
    """ONE-SHOT canonical record before any POST; never advances the Intent."""
    evidence = _evidence(orchestrator, task_id, governance_policy)
    root, index_path, projection = _paths(orchestrator, task_id)
    try:
        with SingleWriterLock(root / "escrow.lock"):
            evidence = _evidence(orchestrator, task_id, governance_policy)
            index = _index(root)
            aid = fingerprint_json({
                "task_id": task_id,
                "grant_id": evidence["grant_id"],
                "scope_authorization_id": evidence["scope_authorization_id"],
                "intent_sha256": evidence["intent_sha256"],
                "operation": "brain_simulation",
            })
            if (aid in index["by_attempt"] or task_id in index["by_task"]
                    or evidence["grant_id"] in index["by_grant"]
                    or projection.exists() or projection.is_symlink()):
                raise OrchestrationError("pre-dispatch escrow attempt already reserved")
            record = {
                "schema_version": _SCHEMA, "attempt_id": aid, **evidence,
                "status": _PREPARED, "permission_to_dispatch": False,
                "automatic_retry_allowed": False,
                "reserved_at": datetime.now(timezone.utc).isoformat(),
            }
            index["by_attempt"][aid] = {
                "task_id": task_id, "grant_id": evidence["grant_id"],
                "record_sha256": fingerprint_json(record),
            }
            index["by_task"][task_id] = aid
            index["by_grant"][evidence["grant_id"]] = aid
            # Cross-file atomicity is NOT provided by a filesystem; publish
            # the canonical anti-replay index FIRST, then its projection.
            # A crash between writes blocks all tasks pending reconciliation.
            atomic_write_json(index_path, index)
            atomic_write_json(projection, record)
            return inspect_pre_dispatch_escrow(
                orchestrator, task_id, governance_policy)
    except WriterLockError as exc:
        raise OrchestrationError("pre-dispatch escrow lock busy") from exc
