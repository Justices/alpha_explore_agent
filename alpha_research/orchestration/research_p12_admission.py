"""P12 read-only production admission audit across P9-P11 evidence.

This report identifies blockers only. It cannot grant permissions, arm a live
session, submit an Alpha, or dispatch a Simulation.
"""
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
    if (record.get("schema_version") != 1 or not isinstance(request, dict)
            or record.get("request_sha256") != _hash(request)
            or record.get("session_id") != result.get("iteration_session_id")
            or request.get("workflow") != task.get("workflow")
            or request.get("objective") != (task.get("request") or {}).get("objective")
            or not isinstance(rounds, list) or not isinstance(budget, dict)
            or type(budget.get("limit")) is not int
            or type(budget.get("used")) is not int
            or budget["used"] != len(rounds)
            or not 0 <= budget["used"] <= budget["limit"]):
        blockers.append("P12_ITERATION_LEDGER_CONTRACT_INVALID")
    else:
        seen = set()
        for index, row in enumerate(rounds, start=1):
            if not isinstance(row, dict):
                blockers.append("P12_ITERATION_ROUND_MALFORMED")
                break
            digest = row.get("round_sha256")
            body = {key: value for key, value in row.items() if key != "round_sha256"}
            if (row.get("round") != index or not isinstance(row.get("round_key"), str)
                    or row["round_key"] in seen or not isinstance(digest, str)
                    or not _HEX64.fullmatch(digest) or _hash(body) != digest):
                blockers.append("P12_ITERATION_ROUND_INTEGRITY_FAILED")
                break
            seen.add(row["round_key"])
    if record.get("status") != "COMPLETED":
        blockers.append("P12_ITERATION_NOT_COMPLETED")
    if not rounds:
        blockers.append("P12_ITERATION_HAS_NO_ROUNDS")
    return {
        "status": record.get("status", "INVALID"),
        "iteration_ref": str(path),
        "iteration_sha256": fingerprint_json(record),
        "round_count": len(rounds) if isinstance(rounds, list) else 0,
        "budget": budget, "stop_reason": record.get("stop_reason"),
        "blockers": sorted(set(blockers)),
    }


def _inspect_candidate_governance(state_root, config, result):
    alpha_ids = _alpha_ids(result)
    pool = CandidatePool(ResearchRecordStore(state_root)).build_index(
        current_policy_sha256=policy_hash(config))
    entries = {row.get("alpha_id"): row for row in pool.get("entries", [])
               if isinstance(row, dict)}
    blockers, audited = [], []
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
        if candidate_status != "CANDIDATE":
            blockers.append("P12_CANDIDATE_NOT_QUALIFIED:" + alpha_id)
        if preferred_status != "PREFERRED":
            blockers.append("P12_PREFERRED_NOT_QUALIFIED:" + alpha_id)
        if row.get("requires_reaudit"):
            blockers.append("P12_CANDIDATE_REAUDIT_REQUIRED:" + alpha_id)
    return {
        "status": "QUALIFIED" if not blockers else "BLOCKED",
        "pool_sha256": fingerprint_json(pool), "alpha_ids": alpha_ids,
        "evaluations": audited, "blockers": sorted(set(blockers)),
    }


def _inspect_recovery(state_root, task):
    target = task.get("recovery_target")
    if not isinstance(target, dict) or not isinstance(target.get("ref"), str):
        return {"status": "NO_RECOVERY_TARGET", "blockers": []}
    coordinator = RecoveryCoordinator(state_root)
    try:
        journal = coordinator.journal_for(target["ref"])
    except (RecoveryError, OSError, ValueError):
        return {"status": "UNVERIFIED", "blockers": ["P12_RECOVERY_TARGET_UNVERIFIED"]}
    if journal is None:
        return {"status": "NO_RECOVERY_JOURNAL", "blockers": []}
    blockers = []
    if journal.get("status") != "COMPLETED":
        blockers.append("P12_RECOVERY_NOT_CLOSED")
    manual_path = (coordinator.journal_root / "manual" /
                   f"{journal.get('recovery_id')}.json")
    if manual_path.is_symlink():
        blockers.append("P12_MANUAL_TAKEOVER_JOURNAL_UNVERIFIED")
        manual = None
    else:
        manual = read_json(manual_path) if manual_path.is_file() else None
        if manual_path.is_file() and not isinstance(manual, dict):
            blockers.append("P12_MANUAL_TAKEOVER_JOURNAL_UNVERIFIED")
    if isinstance(manual, dict) and manual.get("decisions"):
        blockers.append("P12_MANUAL_TAKEOVER_REQUIRES_INDEPENDENT_REVIEW")
    return {
        "status": journal.get("status", "INVALID"),
        "recovery_journal_ref": str(coordinator._journal_path(
            task["workflow"], Path(target["ref"]).resolve())),
        "manual_takeover_ref": str(manual_path) if isinstance(manual, dict) else None,
        "blockers": blockers,
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
