"""Shared, bounded multi-round research iteration protocol.

The controller records immutable round summaries only. Domain workflows keep
ownership of planning, platform contracts, Simulation intents, qualification
and recovery. This module never calls a model or BRAIN.
"""
import hashlib
import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

from alpha_research.state.store import atomic_write_json, read_json

_HEX32 = re.compile(r"[0-9a-f]{32}\\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\\Z")
_TERMINAL = {"COMPLETED", "PARTIAL", "BLOCKED", "STOPPED"}
_PENDING = {"EVIDENCE_PENDING", "PENDING_FULL_AUDIT", "REVIEW_PENDING",
            "CHECKS_PENDING", "PAIRWISE_PENDING", "PENDING_FULL_GATE"}


class IterationError(ValueError):
    """Invalid or conflicting research iteration state."""


def _now(previous=None):
    value = datetime.now(timezone.utc)
    if previous:
        prior = datetime.fromisoformat(previous)
        if value <= prior:
            value = prior + timedelta(microseconds=1)
    return value.isoformat()


def _hash(value):
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str).encode("utf-8")).hexdigest()


def _nonempty(value, name):
    if not isinstance(value, str) or not value.strip():
        raise IterationError(name + " must be a nonempty string")
    return value


def is_stop_requested(stop_flag):
    if stop_flag is None:
        return False
    exists = getattr(stop_flag, "exists", None)
    if callable(exists):
        return bool(exists())
    path = Path(stop_flag)
    return path.exists() or path.is_symlink()


def _candidate_evidence(result):
    candidate_ref = result.get("candidate_ref") or result.get("candidate_record_ref")
    record = None
    if isinstance(candidate_ref, str):
        path = Path(candidate_ref).expanduser()
        if path.is_file() and not path.is_symlink():
            value = read_json(path)
            if isinstance(value, dict):
                record = value
    if record is None:
        return {
            "candidate_status": result.get("candidate_status"),
            "preferred_status": result.get("preferred_status"),
            "failures": result.get("failures") or [],
            "pending": result.get("pending") or [],
            "metrics": {},
        }
    return {
        "candidate_status": record.get("status"),
        "preferred_status": record.get("preferred_status"),
        "failures": record.get("failures") or [],
        "pending": record.get("pending") or [],
        "metrics": record.get("metrics") or {},
    }


def _failure_signature(result):
    evidence = _candidate_evidence(result)
    status = evidence.get("candidate_status")
    if status in (None, "CANDIDATE"):
        return None
    return _hash({
        "candidate_status": status,
        "preferred_status": evidence.get("preferred_status"),
        "failures": sorted(evidence.get("failures") or []),
        "pending": sorted(evidence.get("pending") or []),
        "metrics": evidence.get("metrics") or {},
    })


class IterationLedger:
    """Append-only round ledger with a pinned request and finite budget."""

    def __init__(self, state_root, *, session_id, workflow, objective,
                 scope, source_ref=None, max_iterations=1):
        if not isinstance(session_id, str) or not _HEX32.fullmatch(session_id):
            raise IterationError("iteration session ID must be 32 lowercase hex characters")
        if workflow not in {"regular", "super", "optimization"}:
            raise IterationError("unsupported iteration workflow")
        _nonempty(objective, "objective")
        if not isinstance(scope, dict):
            raise IterationError("iteration scope must be an object")
        maximum = 10000 if workflow == "regular" else 20
        if type(max_iterations) is not int or not 1 <= max_iterations <= maximum:
            raise IterationError(
                f"max_iterations must be an integer from 1 to {maximum}")
        self.root = Path(state_root).resolve()
        self.path = (self.root / "state" / "research_v1_1" /
                     "iterations" / (session_id + ".json"))
        if self.path.is_symlink() or self.path.parent.is_symlink():
            raise IterationError("iteration ledger cannot be a symlink")
        request = {
            "workflow": workflow, "objective": objective,
            "scope": scope, "source_ref": source_ref,
            "max_iterations": max_iterations,
        }
        request_sha256 = _hash(request)
        if self.path.exists():
            saved = read_json(self.path)
            if (not isinstance(saved, dict) or saved.get("schema_version") != 1
                    or saved.get("session_id") != session_id
                    or saved.get("request_sha256") != request_sha256
                    or saved.get("request") != request
                    or not isinstance(saved.get("rounds"), list)):
                raise IterationError("existing iteration ledger identity or request changed")
            if saved.get("status") in _TERMINAL:
                raise IterationError("iteration session is already terminal")
            self.record = saved
        else:
            now = _now()
            self.record = {
                "schema_version": 1, "session_id": session_id,
                "workflow": workflow, "request": request,
                "request_sha256": request_sha256,
                "status": "RUNNING",
                "budget": {"limit": max_iterations, "used": 0, "reserved": 0},
                "rounds": [], "stop_reason": None,
                "created_at": now, "updated_at": now,
            }
            self._save()

    def _save(self):
        self.record["updated_at"] = _now(self.record.get("updated_at"))
        atomic_write_json(self.path, self.record)

    def record_round(self, *, round_key, status, candidate_status=None,
                     preferred_status=None, alpha_ids=None, proposal_sha256=None,
                     definition_sha256=None, failure_signature_sha256=None,
                     evidence_refs=None, reason=None, started_at=None,
                     completed_at=None):
        _nonempty(round_key, "round_key")
        _nonempty(status, "round status")
        refs = evidence_refs or {}
        if (not isinstance(refs, dict) or any(
                not isinstance(key, str) or not key
                or not isinstance(value, str) or not value
                for key, value in refs.items())):
            raise IterationError("round evidence_refs must map names to nonempty references")
        ids = alpha_ids or []
        if (not isinstance(ids, list) or any(not isinstance(item, str) or not item for item in ids)
                or len(ids) != len(set(ids))):
            raise IterationError("round Alpha IDs must be unique nonempty strings")
        for name, digest in (("proposal_sha256", proposal_sha256),
                             ("definition_sha256", definition_sha256),
                             ("failure_signature_sha256", failure_signature_sha256)):
            if digest is not None and (
                    not isinstance(digest, str) or not _HEX64.fullmatch(digest)):
                raise IterationError(name + " must be a SHA-256 digest")
        existing = next((item for item in self.record["rounds"]
                         if item.get("round_key") == round_key), None)
        row = {
            "round": (existing.get("round") if existing is not None
                      else len(self.record["rounds"]) + 1),
            "round_key": round_key, "status": status,
            "candidate_status": candidate_status,
            "preferred_status": preferred_status,
            "alpha_ids": ids,
            "proposal_sha256": proposal_sha256,
            "definition_sha256": definition_sha256,
            "failure_signature_sha256": failure_signature_sha256,
            "evidence_refs": refs, "reason": reason,
            "started_at": started_at or _now(),
            "completed_at": completed_at or _now(),
        }
        row["round_sha256"] = _hash(row)
        if existing is not None:
            if existing.get("round_sha256") != row["round_sha256"]:
                raise IterationError("a recorded iteration round cannot be overwritten")
            return existing
        if self.record["budget"]["used"] >= self.record["budget"]["limit"]:
            raise IterationError("iteration budget is exhausted")
        self.record["rounds"].append(row)
        self.record["budget"]["used"] = len(self.record["rounds"])
        self._save()
        return row

    def mark_recovery_pending(self, reason):
        """Keep an interrupted round resumable by the explicit recovery path."""
        _nonempty(reason, "recovery reason")
        if self.record.get("status") in _TERMINAL:
            raise IterationError("terminal iteration session cannot enter recovery")
        self.record["status"] = "RECOVERY_PENDING"
        self.record["stop_reason"] = reason
        self._save()
        return self.record

    def reconcile_recovery(self, *, recovery_ref, result):
        """Append verified recovery evidence and close the interrupted round."""
        _nonempty(recovery_ref, "recovery_ref")
        if not isinstance(result, dict):
            raise IterationError("recovery result must be an object")
        if self.record.get("status") not in {"RECOVERY_PENDING", "RUNNING"}:
            raise IterationError("iteration session is not awaiting recovery")
        pending_rounds = [row for row in self.record["rounds"]
                          if row.get("status") == "RECOVERY_REQUIRED"]
        if len(pending_rounds) != 1:
            raise IterationError("recovery must bind exactly one interrupted round")
        target = pending_rounds[0]
        refs = {
            key: result[key] for key in (
                "receipt_ref", "readback_ref", "candidate_record_ref",
                "preferred_record_ref", "audit_ref")
            if isinstance(result.get(key), str) and result[key]
        }
        alpha_ids = []
        for key in ("alpha_id", "candidate_alpha_id"):
            value = result.get(key)
            if isinstance(value, str) and value and value not in alpha_ids:
                alpha_ids.append(value)
        rows = self.record.setdefault("reconciliations", [])
        existing = next((row for row in rows
                         if row.get("recovery_ref") == recovery_ref), None)
        result_sha256 = _hash(result)
        if existing is not None:
            if (existing.get("round_key") != target["round_key"]
                    or existing.get("result_sha256") != result_sha256
                    or existing.get("status") != "RECOVERED"):
                raise IterationError("recovery reconciliation cannot be overwritten")
            return self.record
        reconciliation = {
            "round_key": target["round_key"], "recovery_ref": recovery_ref,
            "status": "RECOVERED", "result_sha256": result_sha256,
            "candidate_status": result.get("candidate_status"),
            "preferred_status": result.get("preferred_status"),
            "alpha_ids": alpha_ids, "evidence_refs": refs,
            "created_at": _now(),
        }
        reconciliation["reconciliation_sha256"] = _hash(reconciliation)
        rows.append(reconciliation)
        if self.record["budget"]["used"] >= self.record["budget"]["limit"]:
            self.record["status"] = "COMPLETED"
            reason = "recovered_after_unknown_outcome"
        else:
            self.record["status"] = "PARTIAL"
            reason = "recovered; remaining iteration budget was not automatically resumed"
        self.record["stop_reason"] = reason
        self._save()
        return self.record

    def finish(self, status, reason):
        if status not in _TERMINAL:
            raise IterationError("invalid terminal iteration status")
        _nonempty(reason, "stop_reason")
        if self.record.get("status") in _TERMINAL:
            if self.record.get("status") != status or self.record.get("stop_reason") != reason:
                raise IterationError("terminal iteration result cannot be changed")
            return self.record
        self.record["status"] = status
        self.record["stop_reason"] = reason
        self._save()
        return self.record


def run_bounded_iterations(state_root, *, workflow, objective, scope,
                           source_ref, max_iterations, run_one,
                           stop_flag=None, execution_context=None):
    """Run independent domain rounds with one shared research identity.

    Each domain invocation creates its own immutable run/branch and Simulation
    Intent. The orchestration recovery target is rebound before each sequential
    round, so at most one round can be in flight. Unknown outcomes stop the
    loop and require explicit recovery; they are never automatically retried.
    """
    if not callable(run_one):
        raise IterationError("run_one callback is required")
    if type(max_iterations) is not int or not 1 <= max_iterations <= 20:
        raise IterationError("max_iterations must be an integer from 1 to 20")
    session_id = uuid4().hex
    research_id = workflow + ":" + session_id
    ledger = IterationLedger(
        state_root, session_id=session_id, workflow=workflow,
        objective=objective, scope=scope, source_ref=source_ref,
        max_iterations=max_iterations)
    results = []
    previous_failure = None
    repeated_failure_count = 0
    stop_reason = "iteration_budget_exhausted"
    terminal_status = "COMPLETED"

    for number in range(1, max_iterations + 1):
        if is_stop_requested(stop_flag):
            stop_reason, terminal_status = "user_stop_requested", "STOPPED"
            break
        started = _now()
        try:
            result = run_one(number, research_id)
            if not isinstance(result, dict):
                raise IterationError("domain workflow returned a non-object result")
        except Exception as exc:
            ledger.record_round(
                round_key="iteration:" + str(number), status="RECOVERY_REQUIRED",
                reason=type(exc).__name__, started_at=started,
                completed_at=_now())
            ledger.mark_recovery_pending(
                "iteration_failed_recovery_or_manual_review_required:" +
                type(exc).__name__)
            raise

        evidence = _candidate_evidence(result)
        failure_signature = _failure_signature(result)
        refs = {
            key: result[key] for key in (
                "record_ref", "report_ref", "candidate_ref", "preferred_ref",
                "candidate_record_ref", "preferred_record_ref",
                "simulation_intent_ref", "readback_ref")
            if isinstance(result.get(key), str) and result[key]
        }
        alpha_ids = []
        for key in ("alpha_id", "candidate_alpha_id"):
            value = result.get(key)
            if isinstance(value, str) and value and value not in alpha_ids:
                alpha_ids.append(value)
        ledger.record_round(
            round_key="iteration:" + str(number),
            status=str(result.get("status") or "UNKNOWN"),
            candidate_status=evidence.get("candidate_status"),
            preferred_status=evidence.get("preferred_status"),
            alpha_ids=alpha_ids,
            proposal_sha256=result.get("proposal_sha256"),
            definition_sha256=result.get("candidate_definition_sha256"),
            failure_signature_sha256=failure_signature,
            evidence_refs=refs, started_at=started, completed_at=_now())
        results.append(result)

        if result.get("status") == "DUPLICATE_PROPOSAL_BLOCKED":
            stop_reason, terminal_status = "duplicate_candidate_definition", "PARTIAL"
            break
        if result.get("status") == "USER_STOPPED":
            stop_reason, terminal_status = "user_stop_requested", "STOPPED"
            break
        if result.get("status") == "AWAITING_INDEPENDENT_AUTHORIZATION":
            stop_reason, terminal_status = "independent_authorization_handoff", "PARTIAL"
            break
        if evidence.get("preferred_status") == "PREFERRED":
            stop_reason = "preferred_candidate_reached"
            break
        if (evidence.get("candidate_status") in _PENDING
                or evidence.get("preferred_status") == "PENDING_PREFERRED"
                or result.get("status") in {"outcome_unknown", "unresolved",
                                            "simulation_prepared", "simulation_posting",
                                            "source_integrity_unresolved"}):
            stop_reason = "qualification_or_execution_evidence_unresolved"
            terminal_status = "PARTIAL"
            break
        if failure_signature is not None and failure_signature == previous_failure:
            repeated_failure_count += 1
        else:
            repeated_failure_count = 0
        previous_failure = failure_signature
        if repeated_failure_count >= 1:
            stop_reason = "no_progress_repeated_failure_signature"
            break
    else:
        stop_reason = "iteration_budget_exhausted"

    if is_stop_requested(stop_flag) and stop_reason == "iteration_budget_exhausted":
        stop_reason, terminal_status = "user_stop_requested", "STOPPED"
    record = ledger.finish(terminal_status, stop_reason)
    return {
        **(results[-1] if results else {}),
        "iteration_session_id": session_id,
        "iteration_ref": str(ledger.path),
        "iterations_completed": len(results),
        "iteration_budget": record["budget"],
        "iteration_stop_reason": stop_reason,
        "iteration_results": results,
    }
