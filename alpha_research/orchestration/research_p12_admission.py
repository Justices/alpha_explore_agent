"""P12 read-only production admission audit across P9-P11 evidence.

This report identifies blockers only. It cannot grant permissions, arm a live
session, submit an Alpha, or dispatch a Simulation.
"""
import hashlib
from pathlib import Path

from alpha_research.orchestration.release_gate import inspect_production_release_gate
from alpha_research.orchestration.recovery import RecoveryCoordinator, RecoveryError
from alpha_research.qualification.pool import CandidatePool, policy_hash
from alpha_research.state.fingerprint import fingerprint_json
from alpha_research.state.records import ResearchRecordStore
from alpha_research.state.store import read_json
from alpha_research.workflows.iteration import _hash, _HEX64


def _execution_result(task):
    checkpoints = task.get("checkpoints")
    result = checkpoints.get("execute") if isinstance(checkpoints, dict) else None
    return result if isinstance(result, dict) else {}


def _alpha_ids(result):
    values = []
    def add(value):
        if isinstance(value, str) and value and value not in values:
            values.append(value)
    add(result.get("alpha_id"))
    add(result.get("candidate_alpha_id"))
    for value in result.get("alpha_ids", []):
        add(value)
    for receipt in result.get("completed", []):
        if isinstance(receipt, dict):
            for value in receipt.get("alpha_ids", []):
                add(value)
    for row in result.get("iteration_results", []):
        if isinstance(row, dict):
            add(row.get("alpha_id"))
            add(row.get("candidate_alpha_id"))
            for value in row.get("alpha_ids", []):
                add(value)
    return values


def _inspect_iteration(state_root, task, result):
    reference = result.get("iteration_ref")
    if not isinstance(reference, str) or not reference:
        return {"status": "MISSING", "blockers": ["P12_ITERATION_LEDGER_MISSING"]}
    path = Path(reference).expanduser()
    if path.is_symlink() or path.parent.is_symlink():
        return {"status": "INVALID", "blockers": ["P12_ITERATION_LEDGER_SYMLINK"]}
    path = path.resolve()
    root = (Path(state_root).resolve() / "state" / "research_v1_1" /
            "iterations").resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return {"status": "INVALID", "blockers": ["P12_ITERATION_LEDGER_OUTSIDE_STATE_ROOT"]}
    record = read_json(path)
    if not isinstance(record, dict):
        return {"status": "INVALID", "blockers": ["P12_ITERATION_LEDGER_UNREADABLE"]}
    request, rounds, budget = record.get("request"), record.get("rounds"), record.get("budget")
    blockers = []
    session_id = result.get("iteration_session_id")
    if (record.get("schema_version") != 1
            or record.get("workflow") != task.get("workflow")
            or record.get("session_id") != session_id
            or not isinstance(request, dict)
            or record.get("request_sha256") != _hash(request)
            or request.get("workflow") != task.get("workflow")
            or request.get("objective") != (task.get("request") or {}).get("objective")
            or type(request.get("max_iterations")) is not int
            or not isinstance(rounds, list) or not isinstance(budget, dict)
            or type(budget.get("limit")) is not int
            or type(budget.get("used")) is not int
            or type(budget.get("reserved")) is not int
            or budget.get("limit") != request.get("max_iterations")
            or budget.get("used") != len(rounds)
            or not 1 <= budget.get("limit", 0)
            or not 0 <= budget.get("used", -1) <= budget.get("limit", 0)):
        blockers.append("P12_ITERATION_LEDGER_CONTRACT_INVALID")
    if isinstance(budget, dict) and budget.get("reserved") != 0:
        blockers.append("P12_ITERATION_BUDGET_RESERVATION_UNSETTLED")
    if not isinstance(session_id, str) or len(session_id) != 32:
        blockers.append("P12_ITERATION_SESSION_ID_INVALID")
    else:
        try:
            int(session_id, 16)
        except ValueError:
            blockers.append("P12_ITERATION_SESSION_ID_INVALID")
    if isinstance(rounds, list):
        seen = set()
        for index, row in enumerate(rounds, start=1):
            if not isinstance(row, dict):
                blockers.append("P12_ITERATION_ROUND_MALFORMED")
                break
            digest = row.get("round_sha256")
            body = {key: value for key, value in row.items() if key != "round_sha256"}
            if (row.get("round") != index
                    or not isinstance(row.get("round_key"), str)
                    or not row["round_key"] or row["round_key"] in seen
                    or not isinstance(row.get("status"), str) or not row["status"]
                    or not isinstance(digest, str) or not _HEX64.fullmatch(digest)
                    or _hash(body) != digest):
                blockers.append("P12_ITERATION_ROUND_INTEGRITY_FAILED")
                break
            seen.add(row["round_key"])
    reconciliations = record.get("reconciliations", [])
    if not isinstance(reconciliations, list):
        blockers.append("P12_ITERATION_RECONCILIATIONS_MALFORMED")
        reconciliations = []
    reconciled_keys = set()
    for reconciliation in reconciliations:
        if not isinstance(reconciliation, dict):
            blockers.append("P12_ITERATION_RECONCILIATION_MALFORMED")
            continue
        digest = reconciliation.get("reconciliation_sha256")
        body = {key: value for key, value in reconciliation.items()
                if key != "reconciliation_sha256"}
        round_key = reconciliation.get("round_key")
        recovery_ref = reconciliation.get("recovery_ref")
        result_digest = reconciliation.get("result_sha256")
        if (not isinstance(round_key, str) or round_key in reconciled_keys
                or reconciliation.get("status") != "RECOVERED"
                or not isinstance(recovery_ref, str) or not recovery_ref
                or not isinstance(result_digest, str) or not _HEX64.fullmatch(result_digest)
                or not isinstance(digest, str) or not _HEX64.fullmatch(digest)
                or _hash(body) != digest
                or not any(row.get("round_key") == round_key
                           and row.get("status") == "RECOVERY_REQUIRED"
                           for row in rounds if isinstance(row, dict))):
            blockers.append("P12_ITERATION_RECONCILIATION_INTEGRITY_FAILED")
            continue
        reconciled_keys.add(round_key)
    for row in (rounds if isinstance(rounds, list) else []):
        if (isinstance(row, dict) and row.get("status") == "RECOVERY_REQUIRED"
                and row.get("round_key") not in reconciled_keys):
            blockers.append("P12_ITERATION_RECOVERY_UNRECONCILED")
    if record.get("status") != "COMPLETED":
        blockers.append("P12_ITERATION_NOT_COMPLETED")
    if not isinstance(record.get("stop_reason"), str) or not record["stop_reason"].strip():
        blockers.append("P12_ITERATION_STOP_REASON_MISSING")
    if not rounds:
        blockers.append("P12_ITERATION_HAS_NO_ROUNDS")
    return {
        "status": record.get("status", "INVALID"),
        "iteration_ref": str(path),
        "iteration_sha256": fingerprint_json(record),
        "round_count": len(rounds) if isinstance(rounds, list) else 0,
        "recovery_refs": [row.get("recovery_ref") for row in reconciliations
                          if isinstance(row, dict) and isinstance(row.get("recovery_ref"), str)],
        "budget": budget, "stop_reason": record.get("stop_reason"),
        "blockers": sorted(set(blockers)),
    }

def _inspect_candidate_governance(state_root, config, result):
    alpha_ids = _alpha_ids(result)
    pool = CandidatePool(ResearchRecordStore(state_root)).build_index(
        current_policy_sha256=policy_hash(config))
    entries = {row.get("alpha_id"): row for row in pool.get("entries", [])
               if isinstance(row, dict)}
    blockers, audited, selected = [], [], []
    rejected = {
        "RA_FAILED", "CORR_REJECTED", "PAIRWISE_CONFLICT",
        "PLATFORM_CHECK_FAILED", "METRICS_FAILED",
    }
    pending = {
        "EVIDENCE_PENDING", "PENDING_FULL_AUDIT", "REVIEW_PENDING",
        "CHECKS_PENDING", "PAIRWISE_PENDING", "EVALUATION_PENDING",
        "PENDING_FULL_GATE", "ROBUSTNESS_REVIEW", "TRADEOFF_REVIEW",
        "RA_UNAVAILABLE", "POLICY_STALE",
    }
    if not alpha_ids:
        blockers.append("P12_CANDIDATE_ALPHA_IDS_MISSING")
    for alpha_id in alpha_ids:
        row = entries.get(alpha_id)
        if row is None:
            blockers.append("P12_CANDIDATE_NOT_IN_GLOBAL_POOL:" + alpha_id)
            audited.append({"alpha_id": alpha_id, "candidate_status": "MISSING",
                            "preferred_status": "NOT_CANDIDATE"})
            continue
        candidate_status, preferred_status = (
            row.get("candidate_status"), row.get("preferred_status"))
        audited.append({
            "alpha_id": alpha_id, "candidate_id": row.get("candidate_id"),
            "candidate_status": candidate_status, "preferred_status": preferred_status,
            "requires_reaudit": row.get("requires_reaudit"),
        })
        if row.get("requires_reaudit"):
            blockers.append("P12_CANDIDATE_REAUDIT_REQUIRED:" + alpha_id)
        if candidate_status in pending:
            blockers.append("P12_CANDIDATE_EVIDENCE_UNRESOLVED:" + alpha_id)
        elif candidate_status == "CANDIDATE":
            if preferred_status == "PREFERRED":
                selected.append(alpha_id)
            elif preferred_status == "PENDING_PREFERRED":
                blockers.append("P12_PREFERRED_EVIDENCE_UNRESOLVED:" + alpha_id)
        elif candidate_status not in rejected:
            blockers.append("P12_CANDIDATE_STATUS_UNRECOGNIZED:" + alpha_id)
    if not selected:
        blockers.append("P12_NO_PREFERRED_CANDIDATE")
    return {
        "status": "QUALIFIED" if not blockers else "BLOCKED",
        "pool_sha256": fingerprint_json(pool), "alpha_ids": alpha_ids,
        "selected_preferred_alpha_ids": selected,
        "evaluations": audited, "blockers": sorted(set(blockers)),
    }

def _inspect_recovery(state_root, task):
    current = task.get("recovery_target")
    history = task.get("recovery_target_history")
    if not isinstance(history, list) or not history:
        history = [current] if isinstance(current, dict) else []
    if not history:
        return {"status": "NO_RECOVERY_TARGET", "blockers": []}
    coordinator = RecoveryCoordinator(state_root)
    blockers, inspected, journal_refs = [], [], []
    seen = set()
    expected_kind = ("regular_batch" if task.get("workflow") == "regular"
                     else "simulation_intent")
    if isinstance(current, dict) and history:
        if (history[-1].get("ref") != current.get("ref")
                or history[-1].get("kind") != current.get("kind")):
            blockers.append("P12_RECOVERY_CURRENT_TARGET_HISTORY_MISMATCH")
    for entry in history:
        if not isinstance(entry, dict) or not isinstance(entry.get("ref"), str):
            blockers.append("P12_RECOVERY_TARGET_HISTORY_MALFORMED")
            continue
        reference = entry["ref"]
        if reference in seen:
            blockers.append("P12_RECOVERY_TARGET_HISTORY_DUPLICATED")
            continue
        seen.add(reference)
        if entry.get("kind") not in (None, expected_kind):
            blockers.append("P12_RECOVERY_TARGET_KIND_MISMATCH")
            continue
        try:
            path = coordinator._resolve_target(reference)
            workflow, resolved, intent, target_fingerprint = coordinator._inspect(path)
        except (RecoveryError, OSError, ValueError, TypeError):
            blockers.append("P12_RECOVERY_TARGET_UNVERIFIED")
            inspected.append({"target_ref": reference, "status": "UNVERIFIED"})
            continue
        if workflow != task.get("workflow"):
            blockers.append("P12_RECOVERY_TARGET_WORKFLOW_MISMATCH")
        intent_status = intent.get("status") if isinstance(intent, dict) else None
        if intent_status != "READ_BACK":
            blockers.append("P12_RECOVERY_INTENT_NOT_READ_BACK")
        journal_path = coordinator._journal_path(workflow, resolved)
        journal = None
        if journal_path.is_symlink():
            blockers.append("P12_RECOVERY_JOURNAL_SYMLINK")
        elif journal_path.is_file():
            journal = read_json(journal_path)
            if not isinstance(journal, dict):
                blockers.append("P12_RECOVERY_JOURNAL_UNREADABLE")
                journal = None
        if isinstance(journal, dict):
            journal_refs.append(str(journal_path))
            expected_task_id = task.get("task_id")
            if (journal.get("schema_version") != 1
                    or journal.get("recovery_id") != journal_path.stem
                    or journal.get("workflow") != workflow
                    or journal.get("target_ref") != str(resolved)
                    or journal.get("target_fingerprint") != target_fingerprint
                    or journal.get("task_id") != expected_task_id
                    or journal.get("step") != "execute"):
                blockers.append("P12_RECOVERY_JOURNAL_LINEAGE_MISMATCH")
            if journal.get("status") != "COMPLETED":
                blockers.append("P12_RECOVERY_NOT_CLOSED")
            saved_result = journal.get("result")
            if (not isinstance(saved_result, dict)
                    or journal.get("result_sha256") != fingerprint_json(saved_result)):
                blockers.append("P12_RECOVERY_RESULT_INTEGRITY_FAILED")
            manual_path = (coordinator.journal_root / "manual" /
                           f"{journal.get('recovery_id')}.json")
            manual = None
            if manual_path.is_symlink():
                blockers.append("P12_MANUAL_TAKEOVER_JOURNAL_UNVERIFIED")
            elif manual_path.is_file():
                manual = read_json(manual_path)
                if not isinstance(manual, dict):
                    blockers.append("P12_MANUAL_TAKEOVER_JOURNAL_UNVERIFIED")
                    manual = None
            if isinstance(manual, dict):
                actual_journal_sha = hashlib.sha256(journal_path.read_bytes()).hexdigest()
                if (manual.get("schema_version") != 1
                        or manual.get("recovery_id") != journal.get("recovery_id")
                        or manual.get("workflow") != workflow
                        or manual.get("target_ref") != str(resolved)
                        or manual.get("task_id") != expected_task_id
                        or manual.get("recovery_journal_sha256") != actual_journal_sha
                        or not isinstance(manual.get("decisions"), list)):
                    blockers.append("P12_MANUAL_TAKEOVER_JOURNAL_UNVERIFIED")
                else:
                    decision_ids = set()
                    for index, decision in enumerate(manual["decisions"], start=1):
                        if not isinstance(decision, dict):
                            blockers.append("P12_MANUAL_TAKEOVER_DECISION_MALFORMED")
                            continue
                        operator = decision.get("operator")
                        disposition = decision.get("decision")
                        reason = decision.get("reason")
                        evidence = decision.get("evidence")
                        if (not isinstance(operator, str) or not operator.strip()
                                or disposition not in {"TAKE_OVER", "ABANDON", "ESCALATE"}
                                or not isinstance(reason, str) or not reason.strip()
                                or not isinstance(evidence, list)
                                or decision.get("execution_unblocked") is not False
                                or decision.get("automatic_replay_allowed") is not False
                                or decision.get("simulation_invoked") is not False
                                or decision.get("alpha_submission_invoked") is not False):
                            blockers.append("P12_MANUAL_TAKEOVER_DECISION_MALFORMED")
                            continue
                        expected_id = fingerprint_json({
                            "recovery_id": journal["recovery_id"],
                            "operator": operator, "decision": disposition,
                            "reason": reason, "evidence": evidence,
                            "sequence": index,
                        })[:32]
                        if (decision.get("decision_id") != expected_id
                                or expected_id in decision_ids):
                            blockers.append("P12_MANUAL_TAKEOVER_DECISION_INTEGRITY_FAILED")
                        decision_ids.add(expected_id)
                        for evidence_row in evidence:
                            if not isinstance(evidence_row, dict):
                                blockers.append("P12_MANUAL_TAKEOVER_EVIDENCE_MALFORMED")
                                continue
                            evidence_ref, evidence_sha = (
                                evidence_row.get("ref"), evidence_row.get("sha256"))
                            if (not isinstance(evidence_ref, str)
                                    or not isinstance(evidence_sha, str)
                                    or not _HEX64.fullmatch(evidence_sha)):
                                blockers.append("P12_MANUAL_TAKEOVER_EVIDENCE_MALFORMED")
                                continue
                            evidence_path = Path(evidence_ref).expanduser()
                            if (evidence_path.is_symlink() or evidence_path.parent.is_symlink()
                                    or not evidence_path.resolve().is_relative_to(
                                        Path(state_root).resolve())
                                    or not evidence_path.is_file()):
                                blockers.append("P12_MANUAL_TAKEOVER_EVIDENCE_UNVERIFIED")
                                continue
                            actual_sha = hashlib.sha256(
                                evidence_path.read_bytes()).hexdigest()
                            if actual_sha != evidence_sha:
                                blockers.append("P12_MANUAL_TAKEOVER_EVIDENCE_HASH_MISMATCH")
                    if manual["decisions"]:
                        blockers.append("P12_MANUAL_TAKEOVER_REQUIRES_INDEPENDENT_REVIEW")
        inspected.append({
            "target_ref": str(resolved), "workflow": workflow,
            "intent_status": intent_status,
            "recovery_journal_ref": str(journal_path) if journal_path.is_file() else None,
            "recovery_status": journal.get("status") if isinstance(journal, dict) else None,
        })
    return {
        "status": "CLOSED" if not blockers else "BLOCKED",
        "targets": inspected,
        "recovery_journal_ref": journal_refs[-1] if journal_refs else None,
        "recovery_journal_refs": journal_refs,
        "blockers": sorted(set(blockers)),
    }

def inspect_p12_production_admission(orchestrator, task_id, model_config,
                                     governance_policy, *, config_source,
                                     platform_read_evidence_id=None):
    """Verify iteration, qualification and recovery evidence without side effects."""
    task = orchestrator.load(task_id)
    state_root = orchestrator.state_root
    baseline = inspect_production_release_gate(
        orchestrator, task_id, model_config, governance_policy,
        config_source=config_source,
        platform_read_evidence_id=platform_read_evidence_id)
    blockers = set(baseline.get("blockers", []))
    if task.get("status") != "COMPLETED":
        blockers.add("P12_ORCHESTRATION_TASK_NOT_COMPLETED")
    if any(value != "DONE" for value in (task.get("steps") or {}).values()):
        blockers.add("P12_ORCHESTRATION_STEPS_NOT_COMPLETE")
    result = _execution_result(task)
    iteration = _inspect_iteration(state_root, task, result)
    candidate = _inspect_candidate_governance(state_root, model_config, result)
    recovery = _inspect_recovery(state_root, task)
    blockers.update(iteration.get("blockers", []))
    blockers.update(candidate.get("blockers", []))
    blockers.update(recovery.get("blockers", []))
    recovery_ref = recovery.get("recovery_journal_ref")
    if recovery_ref is not None and any(
            value != recovery_ref for value in iteration.get("recovery_refs", [])):
        blockers.add("P12_ITERATION_RECOVERY_REF_MISMATCH")
    blockers.update({
        "P12_EXTERNAL_PRODUCTION_ATTESTATION_MISSING",
        "P12_INDEPENDENT_PLATFORM_WRITE_AUTHORITY_MISSING",
        "P12_SIMULATION_AND_ALPHA_SUBMISSION_REQUIRE_SEPARATE_APPROVAL",
        "P12_PRODUCTION_DISPATCH_REMAINS_DISABLED",
    })
    reconciliation = any(
        "RECONCILIATION" in item or "RECOVERY_NOT_CLOSED" in item
        or "RECOVERY_TARGET_UNVERIFIED" in item for item in blockers)
    report = {
        "schema_version": 1, "task_id": task_id,
        "workflow": task.get("workflow"),
        "status": ("P12_RECONCILIATION_BLOCKED" if reconciliation
                   else "P12_PRODUCTION_BLOCKED"),
        "audit_mode": "READ_ONLY",
        "components": {
            "baseline_release_gate": baseline,
            "p9_iteration": iteration,
            "p10_candidate_governance": candidate,
            "p11_recovery": recovery,
        },
        "blockers": sorted(blockers),
        "production_authorized": False, "model_call_authorized": False,
        "brain_read_authorized": False, "brain_simulation_authorized": False,
        "alpha_submission_authorized": False, "automatic_retry_allowed": False,
        "permission_to_dispatch": False, "brain_invoked": False,
        "provider_invoked": False,
    }
    report["audit_sha256"] = fingerprint_json(report)
    return report
