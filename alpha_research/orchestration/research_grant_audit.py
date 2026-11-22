"""P4.17 durable *audit-only* research grant claim, never an execution token.

The canonical journal is atomic and protected by an OS writer lock. It
prevents a locally verified grant, Task, or approval ID from being reserved
twice in this state root. Local storage is not an independently trusted
global grant consumer; cross-machine claims need an external issuer service.
No paid model/BRAIN interface or executable dispatch capability is present.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_only_admission import verify_research_grant
from alpha_research.state.sealed import is_sealed_state, write_sealed_json
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError

_VERSION = 1
_STATUS = "RESERVED_RESEARCH_GRANT_AUDIT_ONLY"
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_ENTRY = frozenset({
    "task_id", "grant_id", "operator_approval_id",
    "receipt_sha256", "request_sha256",
    "price_evidence_sha256", "reservation_at", "status",
})
_LEDGER = frozenset({"schema_version", "by_task", "state_sha256"})


def _paths(orchestrator):
    root = Path(orchestrator.state_root) / "state" / "research_v1_1" / "research_grant_audit"
    return root, root / "claims.json", root / "claims.lock"


def _safe_paths(root, journal, lock):
    if (root.is_symlink() or root.parent.is_symlink()
            or (root.exists() and not root.is_dir())
            or journal.is_symlink() or lock.is_symlink()):
        raise OrchestrationError("research grant claim journal path unsafe")


def _read_ledger(root, journal):
    if not journal.exists():
        return {"schema_version": _VERSION, "by_task": {}}
    try:
        raw = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OrchestrationError("research grant audit journal unreadable") from exc
    if (not isinstance(raw, dict) or set(raw) != _LEDGER
            or type(raw["schema_version"]) is not int
            or raw["schema_version"] != _VERSION
            or not isinstance(raw["by_task"], dict)
            or not is_sealed_state(raw)):
        raise OrchestrationError("research grant audit journal corruption")
    grant_ids, approval_ids = set(), set()
    for tid, row in raw["by_task"].items():
        if (not isinstance(tid, str) or _HEX32.fullmatch(tid) is None
                or not isinstance(row, dict) or set(row) != _ENTRY
                or row["task_id"] != tid or row["status"] != _STATUS
                or any(not isinstance(row.get(k), str)
                       or _HEX32.fullmatch(row[k]) is None
                       for k in ("grant_id", "operator_approval_id"))
                or any(not isinstance(row.get(k), str)
                       or _HEX64.fullmatch(row[k]) is None
                       for k in ("receipt_sha256", "request_sha256",
                                 "price_evidence_sha256"))
                or not isinstance(row.get("reservation_at"), str)
                or not row["reservation_at"]):
            raise OrchestrationError("research grant audit entry invalid")
        if (row["grant_id"] in grant_ids
                or row["operator_approval_id"] in approval_ids):
            raise OrchestrationError("research grant audit duplicate global IDs")
        grant_ids.add(row["grant_id"])
        approval_ids.add(row["operator_approval_id"])
    return raw


def inspect_research_grant_claim(orchestrator, task_id):
    """Read-only, negative-only projection; never opens BRAIN or a model."""
    task = orchestrator.load(task_id)
    root, journal, lock = _paths(orchestrator)
    base = {
        "schema_version": _VERSION, "task_id": task_id,
        "permission_to_dispatch": False,
        "model_call_authorized": False,
        "brain_read_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "automatic_retry_allowed": False,
        "provider_invoked": False, "brain_invoked": False,
    }
    try:
        _safe_paths(root, journal, lock)
        ledger = _read_ledger(root, journal)
    except OrchestrationError:
        return {**base, "status": "RECONCILIATION_REQUIRED"}
    row = ledger["by_task"].get(task_id)
    if row is None:
        return {**base, "status": "NOT_RESERVED"}
    if row["request_sha256"] != task["request_sha256"]:
        return {**base, "status": "RECONCILIATION_REQUIRED"}
    return {**base, "status": _STATUS, "grant_id": row["grant_id"],
            "operator_approval_id": row["operator_approval_id"],
            "receipt_sha256": row["receipt_sha256"],
            "price_evidence_sha256": row["price_evidence_sha256"]}


def reserve_research_grant_audit(orchestrator, task_id, model_config,
                                 governance_policy, receipt, trust_policy,
                                 *, now=None):
    """Single-use local audit reservation. Never authorizes a provider call."""
    checked = verify_research_grant(
        orchestrator, task_id, model_config, governance_policy,
        receipt, trust_policy, now=now)
    root, journal, lock = _paths(orchestrator)
    _safe_paths(root, journal, lock)
    try:
        with SingleWriterLock(lock):
            _safe_paths(root, journal, lock)
            # Recheck expiry, revocation and immutable identity *under lock*.
            checked = verify_research_grant(
                orchestrator, task_id, model_config, governance_policy,
                receipt, trust_policy, now=now)
            ledger = _read_ledger(root, journal)
            if task_id in ledger["by_task"]:
                raise OrchestrationError("research Task grant already reserved")
            if any(
                row["grant_id"] == checked["grant_id"]
                or row["operator_approval_id"] == checked["operator_approval_id"]
                for row in ledger["by_task"].values()
            ):
                raise OrchestrationError("research grant/approval already reserved")
            task = orchestrator.load(task_id)
            entry = {
                "task_id": task_id,
                "grant_id": checked["grant_id"],
                "operator_approval_id": checked["operator_approval_id"],
                "receipt_sha256": checked["receipt_sha256"],
                "request_sha256": task["request_sha256"],
                "price_evidence_sha256": checked["price_evidence_sha256"],
                "reservation_at": datetime.now(timezone.utc).isoformat(),
                "status": _STATUS,
            }
            ledger["by_task"][task_id] = entry
            # Canonical, single-file atomic claim. No incomplete projections.
            write_sealed_json(journal, ledger)
            return inspect_research_grant_claim(orchestrator, task_id)
    except WriterLockError as exc:
        raise OrchestrationError(
            "research grant claim ledger busy; no automatic retry") from exc
