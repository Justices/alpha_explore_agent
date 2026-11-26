"""Bounded, serial execution of a discovered Regular Campaign."""

import hashlib
import json
import re
import shutil
from tempfile import mkdtemp
from datetime import datetime, timezone, timedelta
from itertools import zip_longest
from pathlib import Path

from alpha_research.workflows.regular.scope import discover_regular_scope, regular_delay
from alpha_research.workflows.regular.campaign_report import (
    current_campaign_findings, prior_campaign_findings, write_campaign_summary,
)
from alpha_research.workflows.iteration import IterationLedger, _hash as _iteration_hash
from alpha_research.workflows.regular.dataset_review import review_dataset
from alpha_research.qualification.ppa import ppa_requested
from alpha_research.state.store import (
    atomic_write_json, has_only_completed_probes, is_pre_submission_draft, read_json,
)
from alpha_research.state.records import ResearchRecordStore
from alpha_research.progress import write_progress


def _now():
    return datetime.now(timezone.utc).isoformat()


def _key(request):
    raw = json.dumps(request, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def load_saved_campaign(state_root, campaign_id):
    if not isinstance(campaign_id, str) or not re.fullmatch(r"[0-9a-f]{32}", campaign_id):
        raise ValueError("Campaign ID 无效")
    path = Path(state_root) / "state/research_v1_1/campaign_runs" / f"{campaign_id}.json"
    state = read_json(path, default={})
    if not isinstance(state, dict) or state.get("campaign_id") != campaign_id or state.get("deleted_at"):
        raise ValueError("Campaign 不存在或已删除")
    return path, state


def campaign_can_resume(state):
    budget = state.get("budget", {})
    reviews = state.get("dataset_reviews", {})
    return (state.get("active_item") is not None
            or budget.get("used", {}).get("batches", 0) < budget.get("limits", {}).get("batches", 0)
            or any(_needs_dataset_review(receipt, reviews) for receipt in state.get("completed", [])))


def _needs_dataset_review(receipt, reviews):
    return (bool(receipt.get("attribution_status") or receipt.get("reconciled"))
            and reviews.get(receipt["dataset_id"], {}).get("status")
            not in {"complete", "no_selection", "optimization_deferred"})


def delete_saved_campaign(state_root, campaign_id):
    """Remove the files owned by this Campaign while the caller holds the writer lock."""
    root = Path(state_root).resolve()
    path, state = load_saved_campaign(root, campaign_id)
    store = ResearchRecordStore(root)
    research_id = f"regular:campaign:{campaign_id}"
    owned = {
        path, path.with_name(f"{campaign_id}_scope_plan.json"),
        path.with_name(f"{campaign_id}_summary.json"),
        root / "state" / "research_v1_1" / "iterations" / f"{campaign_id}.json",
        path.parent / "field_pools" / campaign_id,
        path.parent / "dataset_reviews" / campaign_id,
        root / "state/codex_threads" / hashlib.sha256(research_id.encode()).hexdigest(),
        store.path_for("campaign", campaign_id),
    }
    for item in state.get("items", []):
        subtask_id = _key({"campaign_id": campaign_id, "dataset_id": item["dataset_id"]})
        combination_id = _key({"subtask_id": subtask_id, **{
            key: item[key] for key in ("region", "delay", "universe")}})
        owned.update((store.path_for("dataset_subtask", subtask_id),
                      store.path_for("combination", combination_id)))
    batches = []
    for context_path in (root / "runs").glob("*/*/*/*/*/batch_context.json"):
        context = read_json(context_path, default={})
        if not isinstance(context, dict) or context.get("research_id") != research_id:
            continue
        batch = context_path.parent
        batches.append(batch)
        owned.add(batch)
        for intent_path in batch.rglob("*_intent.json"):
            intent = read_json(intent_path, default={})
            if not isinstance(intent, dict) or intent.get("status") not in {
                    "READ_BACK", "PREPARED", "STOPPED_BEFORE_SUBMISSION"}:
                raise ValueError(f"回测提交状态待确认，请先恢复 {batch.name}")
        case_key, run_id = context.get("platform_case_key"), context.get("platform_run_id")
        if all(isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_-]+", key)
               for key in (case_key, run_id)):
            session = root / "state/platform/cases" / case_key / "sessions" / run_id
            for marker_path in (session / "batches").glob("*.post_started.json"):
                marker = read_json(marker_path, default={})
                progress = read_json(marker_path.with_suffix("").with_suffix(".progress.json"), default={})
                if (not isinstance(marker, dict) or not isinstance(progress, dict)
                        or progress.get("status") != "COMPLETE"
                        or not marker.get("request_sha256")
                        or progress.get("request_sha256") != marker["request_sha256"]):
                    raise ValueError(f"BRAIN 回执待确认，请先恢复 {batch.name}")
            owned.add(session)
    for kind in ("candidate", "preferred"):
        for record_path in (store.root / kind).glob("*.json"):
            body = record_path.read_text(encoding="utf-8").casefold()
            if any(json.dumps(str(batch), ensure_ascii=False)[1:-1].casefold() in body
                   for batch in batches):
                raise ValueError("此 Campaign 的文件仍被候选或优选记录引用")
    for ref in (root / "state/logs/workbench").glob("*.campaign.json"):
        if read_json(ref) == {"campaign_id": campaign_id}:
            launch_id = ref.name.removesuffix(".campaign.json")
            owned.update((ref, ref.with_name(launch_id + ".jsonl"),
                          ref.with_name(launch_id + ".launch.json")))
    for ref in (root / "state/logs/model_calls").glob("*.record.json"):
        record = read_json(ref, default={})
        if isinstance(record, dict) and record.get("research_id") == research_id:
            call_id = ref.name.removesuffix(".record.json")
            owned.update(ref.with_name(f"{call_id}.{kind}.json")
                         for kind in ("record", "context", "response"))
            session_id = record.get("session_id")
            if isinstance(session_id, str) and re.fullmatch(r"[0-9a-f]{32}", session_id):
                owned.add(root / "state/logs/model_sessions" / f"{session_id}.json")
    for target in owned:
        if not target.is_relative_to(root) or target.resolve() != target:
            raise ValueError("研究文件路径超出所属目录")
    staging = Path(mkdtemp(prefix=".delete-campaign-", dir=root))
    moved = []
    try:
        for target in [*sorted(owned - {path}), path]:
            if target.exists():
                destination = staging / target.relative_to(root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                target.rename(destination)
                moved.append((target, destination))
    except OSError:
        for target, destination in reversed(moved):
            destination.rename(target)
        shutil.rmtree(staging)
        raise
    shutil.rmtree(staging)


def _work_items(plan):
    by_dataset = [
        [{"dataset_id": dataset["dataset_id"], "category": dataset["category"],
          "region": combo["region"], "delay": combo["delay"], "universe": combo["universe"]}
         for combo in dataset["combinations"]]
        for dataset in plan["datasets"]]
    return [item for round_items in zip_longest(*by_dataset)
            for item in round_items if item is not None]


def _subtask_summary(state):
    groups = {}
    completed = {entry["item_index"] for entry in state["completed"]}
    fully_completed = {entry["item_index"] for entry in state["completed"]
                       if entry.get("field_research_status") != "partial"}
    failures = {}
    for failure in state.get("failures", []):
        failures.setdefault(failure["item_index"], []).append(failure)
    for index, item in enumerate(state["items"]):
        group = groups.setdefault(item["dataset_id"], {
            "dataset_id": item["dataset_id"], "combinations": [],
            "completed_batches": 0, "status": "eligible"})
        if index == state.get("active_item"):
            status = state["status"]
            group["status"] = status
        elif index in completed:
            status = "completed" if index in fully_completed else "partial"
            if status == "partial" and group["status"] == "eligible":
                group["status"] = "partial"
        elif index in failures:
            status = "unresolved"
            group["status"] = "unresolved"
        elif state["status"] in {"budget_complete", "scope_complete", "partial", "unresolved"}:
            status = "skipped_budget"
        else:
            status = "eligible"
        group["combinations"].append({
            "region": item["region"], "delay": item["delay"],
            "universe": item["universe"], "status": status,
            "failure_history": failures.get(index, [])})
    for entry in state["completed"]:
        groups[entry["dataset_id"]]["completed_batches"] += 1
    for group in groups.values():
        review = state.get("dataset_reviews", {}).get(group["dataset_id"])
        if review:
            group["dataset_review"] = review
        if group["status"] != "eligible":
            continue
        if all(item["status"] == "completed" for item in group["combinations"]):
            group["status"] = "covered"
        elif state["status"] in {"budget_complete", "scope_complete", "partial", "unresolved"}:
            group["status"] = "partial_budget"
    return [groups[key] for key in sorted(groups)]


def _persist_record(store, kind, record):
    """Upsert a versioned record without timestamp regressions."""
    identity = {
        "campaign": "campaign_id",
        "dataset_subtask": "subtask_id",
        "combination": "combination_id",
    }[kind]
    try:
        previous = store.get(kind, record[identity])
    except FileNotFoundError:
        previous = None
    if previous is not None and {key: value for key, value in previous.items()
                                 if key not in {"created_at", "updated_at"}} == record:
        return
    now = datetime.now(timezone.utc)
    if previous is not None:
        last = datetime.fromisoformat(previous["updated_at"])
        if now <= last:
            now = last + timedelta(microseconds=1)
        record["created_at"] = previous["created_at"]
    else:
        record["created_at"] = now.isoformat()
    record["updated_at"] = now.isoformat()
    return store.put(
        kind, record,
        expected_updated_at=previous["updated_at"] if previous else None)


def _checkpoint_campaign(state, state_root, path):
    """Commit the Campaign before updating its derived records."""
    store = ResearchRecordStore(state_root)
    summaries = _subtask_summary(state)
    completed = {entry["item_index"] for entry in state["completed"]}
    subtask_ids = []
    records = []
    for summary in summaries:
        subtask_id = _key({"campaign_id": state["campaign_id"],
                           "dataset_id": summary["dataset_id"]})
        subtask_ids.append(subtask_id)
        summary["subtask_id"] = subtask_id
        combination_ids = []
        for index, item in enumerate(state["items"]):
            if item["dataset_id"] != summary["dataset_id"]:
                continue
            combination_id = _key({
                "subtask_id": subtask_id,
                "region": item["region"],
                "delay": item["delay"], "universe": item["universe"]})
            combination_ids.append(combination_id)
            combination = summary["combinations"][len(combination_ids) - 1]
            combination["combination_id"] = combination_id
            support_ref = state["plan_evidence_ref"]
            effective = {
                "dataset_id": item["dataset_id"], "region": item["region"],
                "delay": item["delay"], "universe": item["universe"],
                "category": item["category"]}
            receipts = [
                entry for entry in state["completed"]
                if entry["item_index"] == index]
            records.append(("combination", {
                "schema_version": 1, "combination_id": combination_id,
                "subtask_id": subtask_id, "effective_settings": effective,
                "support_evidence_ref": support_ref, "status": combination["status"],
                "failure_history": combination["failure_history"],
                "batch_refs": [entry["batch_dir"] for entry in receipts],
                "candidate_statuses": [
                    entry["candidate_status"] for entry in receipts],
                "completed_batches": len(receipts)}))
        summary["combination_ids"] = combination_ids
        records.append(("dataset_subtask", {
            "schema_version": 1, "subtask_id": subtask_id,
            "campaign_id": state["campaign_id"],
            "dataset_id": summary["dataset_id"],
            "combination_ids": combination_ids,
            "combinations": summary["combinations"],
            "completed_batches": summary["completed_batches"],
            "status": summary["status"]}))
    state["subtasks"] = summaries
    request = state["request"]
    records.append(("campaign", {
        "schema_version": 2, "campaign_id": state["campaign_id"],
        "workflow": "regular",
        "request": {
            "objective": request["objective"], "region": request["region"],
            "dataset_ids": request["dataset_ids"],
            "category": request["category"], "delay": request["delay"],
            "universe": request["universe"],
            "profile": "ppa" if request["ppa_profile"] else "regular"},
        "scope_revision": 1,
        "budget": state["budget"],
        "dataset_subtask_ids": subtask_ids,
        "plan_evidence_ref": state["plan_evidence_ref"],
        "status": state["status"],
        "stop_reason": state.get("error") or (
            state["status"] if state["status"] in
            ("budget_complete", "scope_complete", "partial", "user_stopped", "unresolved") else None),
        "coverage": {
            "eligible_combinations": len(state["items"]),
            "completed_combinations": len(completed),
            "unexamined_combinations": len(state["items"]) - len(completed),
            "unselected_dataset_ids":
                state["plan"].get("unselected_dataset_ids", []),
            "unresolved": state["plan"].get("unresolved", [])}}))
    state["updated_at"] = _now()
    atomic_write_json(path, state)
    failed_records = 0
    for kind, record in records:
        try:
            _persist_record(store, kind, record)
        except (OSError, ValueError):
            failed_records += 1
    if failed_records:
        write_progress(f"Campaign 已保存，{failed_records} 条派生记录待同步")


def _reconcile_active(agent, state, recovery_batch=None):
    index = state["active_item"]
    item = state["items"][index]
    directory = (Path(agent.runs_root) / item["region"] /
                 f"D{item['delay']}" / item["universe"] / item["dataset_id"])
    if recovery_batch is not None and recovery_batch.parent != directory.resolve():
        raise ValueError("bound recovery batch is outside the active Campaign scope")
    context_paths = (directory.glob("*/batch_context.json") if recovery_batch is None
                     else [recovery_batch / "batch_context.json"])
    matches = []
    research_id = f"regular:campaign:{state['campaign_id']}"
    for context_path in context_paths:
        try:
            context = json.loads(context_path.read_text(encoding="utf-8"))
            started = datetime.fromisoformat(context.get("started_at", ""))
            active = datetime.fromisoformat(state["active_started_at"])
            if (context.get("objective") == state["request"]["objective"]
                    and started >= active
                    and context.get("research_id") in (None, research_id)):
                matches.append((context_path.parent, context))
        except (OSError, ValueError):
            raise ValueError("interrupted Campaign context is unreadable")
    if len(matches) > 1:
        raise ValueError("multiple possible interrupted batches; manual reconciliation required")
    if matches and matches[0][1].get("research_id") == research_id:
        saved_batch = matches[0][0]
        if (saved_batch / "field_research_plan.json").exists():
            return saved_batch
        if recovery_batch is None and (saved_batch / "simulation_intent.json").exists():
            return saved_batch
    if matches and recovery_batch is None:
        batch_dir, context = matches[0]
        if context.get("research_id") == research_id and is_pre_submission_draft(batch_dir):
            if not (batch_dir / "void.json").exists():
                agent.void_batch(batch_dir, "Campaign retry after interruption before submission")
            write_progress(f"已归档本 Campaign 未提交回测的草稿，将重新规划 · {batch_dir}")
            matches.clear()
        elif context.get("research_id") == research_id and has_only_completed_probes(batch_dir):
            return batch_dir
    if not matches:
        if recovery_batch is not None:
            raise ValueError("bound recovery batch does not match the active Campaign item")
        # No batch was started, or the matched draft never reached submission.
        state["active_item"] = None
        state.pop("active_started_at", None)
        state["budget"]["reserved"]["batches"] = 0
        state["status"] = "planned"
        state.pop("error", None)
        return
    batch_dir, _ = matches[0]
    intent_path = batch_dir / "simulation_intent.json"
    result_path = batch_dir / "results.json"
    try:
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError(f"interrupted batch requires recovery: {batch_dir}")
    if intent.get("status") != "READ_BACK":
        raise ValueError(f"interrupted batch requires recovery: {batch_dir}")
    state["completed"].append({
        "item_index": index, "dataset_id": item["dataset_id"],
        "batch_dir": str(batch_dir),
        "alpha_ids": result.get("alpha_ids", []),
        "candidate_status": result.get("candidate_status") or "pending_reaudit",
        "completed_at": _now(), "reconciled": True})
    state["active_item"] = None
    state.pop("active_started_at", None)
    state["budget"]["reserved"]["batches"] = 0
    state["budget"]["used"]["batches"] += 1
    state["status"] = "active"
    state.pop("error", None)


def _record_attempt_failure(state, reason, batch_ref=None):
    used = state["budget"]["used"]
    failure = {
        "item_index": state["active_item"], "attempt": used["batches"] + 1,
        "started_at": state.pop("active_started_at", None),
        "reason": reason, "recorded_at": _now()}
    if isinstance(batch_ref, Path):
        failure["batch_ref"] = str(batch_ref)
    state.setdefault("failures", []).append(failure)
    used["batches"] += 1
    state["budget"]["reserved"]["batches"] = 0
    state["active_item"] = None
    state["status"] = "active"
    state.pop("error", None)


def _sync_iteration_ledger(state, ledger):
    """Idempotently mirror durable Campaign attempts into the shared P9 ledger."""
    attempts = []
    for receipt in state.get("completed", []):
        batch_ref = receipt.get("batch_dir")
        if not isinstance(batch_ref, str) or not batch_ref:
            continue
        batch = Path(batch_ref)
        result = read_json(batch / "results.json", default={})
        prereg = read_json(batch / "prereg.json", default={})
        context = read_json(batch / "batch_context.json", default={})
        proposal = prereg.get("proposal")
        variants = prereg.get("variants")
        proposal_sha = (_iteration_hash(proposal)
                        if isinstance(proposal, dict) else None)
        definition_sha = (_iteration_hash([
            row.get("expression") for row in variants if isinstance(row, dict)])
            if isinstance(variants, list) else None)
        gate_rows = [row for row in (result.get("gate") or {}).values()
                     if isinstance(row, dict)]
        candidate_status = (
            "CANDIDATE" if any(row.get("status") == "CANDIDATE"
                               for row in gate_rows) else
            next((row.get("status") for row in gate_rows), None))
        attempts.append((receipt.get("completed_at") or "", {
            "round_key": batch_ref, "status": "COMPLETED",
            "candidate_status": candidate_status,
            "alpha_ids": receipt.get("alpha_ids", []),
            "proposal_sha256": proposal_sha,
            "definition_sha256": definition_sha,
            "evidence_refs": {
                "batch": batch_ref, "results": str(batch / "results.json"),
                "preregistration": str(batch / "prereg.json")},
            "started_at": context.get("started_at") or receipt.get("completed_at"),
            "completed_at": receipt.get("completed_at"),
        }))
    for failure in state.get("failures", []):
        attempt = failure.get("attempt")
        if type(attempt) is not int:
            continue
        reason = failure.get("reason") or "unresolved_attempt"
        evidence_refs = ({"batch": str(failure["batch_ref"])}
                         if isinstance(failure.get("batch_ref"), (str, Path)) else {})
        attempts.append((failure.get("recorded_at") or "", {
            "round_key": f"failure:{failure.get('item_index')}:{attempt}",
            "status": "FAILED", "reason": str(reason)[:300],
            "failure_signature_sha256": _iteration_hash({"reason": reason}),
            "evidence_refs": evidence_refs,
            "started_at": failure.get("started_at") or failure.get("recorded_at"),
            "completed_at": failure.get("recorded_at"),
        }))
    for _, entry in sorted(attempts, key=lambda value: value[0]):
        ledger.record_round(**entry)


def run_regular_campaign(agent, gateway, *, objective, region, dataset_ids=None,
                         category=None, delay=None, universe=None,
                         max_batches=None, ppa_profile=False, execution_context=None,
                         recovery_batch=None, resume_campaign_id=None,
                         saved_pilot=False):
    """Execute every eligible scope item by default.

    Persist progress before and after each batch. An interrupted batch is
    unresolved until the existing recovery command has reconciled it.
    """
    if saved_pilot and (recovery_batch is None or gateway is not None):
        raise ValueError("saved Pilot recovery requires a bound batch and no platform gateway")
    if max_batches is not None and (type(max_batches) is not int or max_batches < 1):
        raise ValueError("max_batches must be positive")
    if not isinstance(objective, str) or not objective.strip():
        raise ValueError("objective is required")
    if recovery_batch is None:
        regular_delay(delay)
    request = {"objective": objective, "region": region,
               "dataset_ids": dataset_ids, "category": category,
               "delay": delay, "universe": universe,
               "max_batches": max_batches, "ppa_profile": ppa_profile}
    legacy_id = _key(request)
    campaign_id = _key({**request, "batch_budget": "per_dataset"})
    if recovery_batch is not None:
        context = read_json(Path(recovery_batch) / "batch_context.json", default={})
        if isinstance(context, dict) and context.get("research_id") in (
                None, f"regular:campaign:{legacy_id}"):
            campaign_id = legacy_id
    if resume_campaign_id is not None:
        if resume_campaign_id not in (campaign_id, legacy_id):
            raise ValueError("saved Campaign ID does not match its request")
        campaign_id = resume_campaign_id
    research_id = f"regular:campaign:{campaign_id}"
    emit_callback = (execution_context or {}).get("emit_event")
    def emit_event(event_type, payload):
        if callable(emit_callback):
            emit_callback(event_type, payload)

    allow_optimization = (execution_context or {}).get("allow_dataset_optimization", True)
    if execution_context is not None:
        bind_identity = execution_context.get("bind_research_identity")
        if callable(bind_identity):
            bind_identity(research_id)
    state_root = Path(agent.runs_root).resolve().parent
    path = (state_root / "state" /
            "research_v1_1" / "campaign_runs" / f"{campaign_id}.json")
    if recovery_batch is not None:
        recovery_batch = Path(recovery_batch).resolve()
    if (recovery_batch is not None or resume_campaign_id is not None) and not path.exists():
        raise ValueError("Campaign state is missing; recovery cannot reconstruct it safely")
    resume_batch = None
    scope_options = None
    fresh_dataset_proofs = {}
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("deleted_at"):
            raise ValueError("Campaign 已删除，请使用新的研究目标创建任务")
        if "plan_evidence_ref" not in state:
            evidence_path = path.with_name(f"{campaign_id}_scope_plan.json")
            atomic_write_json(evidence_path, state["plan"])
            state["plan_evidence_ref"] = str(evidence_path)
        if state.get("request") != request:
            raise ValueError("saved Campaign request does not match")
        active_before = state.get("active_item")
        if saved_pilot and (active_before is not None
                            or len(state["completed"]) != 1
                            or state["budget"]["limits"]["batches"] != 1
                            or state["budget"]["used"]["batches"] != 1
                            or state["budget"]["reserved"]["batches"] != 0
                            or state.get("failures")):
            raise ValueError("saved Pilot requires one settled Campaign receipt")
        if active_before is not None:
            if "failures" not in state and state.get("error"):
                state["failures"] = [{"item_index": active_before,
                                      "reason": state["error"],
                                      "recorded_at": state["updated_at"]}]
            try:
                resume_batch = _reconcile_active(agent, state, recovery_batch)
            except ValueError as exc:
                if recovery_batch is not None or (execution_context and state["budget"]["limits"]["batches"] == 1):
                    raise
                _record_attempt_failure(state, str(exc))
                write_progress("先前批次需要单独恢复，继续处理剩余预算")
            _checkpoint_campaign(state, state_root, path)
        if recovery_batch is not None and resume_batch is None:
            if not any(Path(entry["batch_dir"]).resolve() == recovery_batch
                       for entry in state["completed"]):
                raise ValueError("bound recovery batch is missing from Campaign receipts")
            if saved_pilot:
                receipt = state["completed"][0]
                dataset_id = receipt["dataset_id"]
                if [dataset_id] != request["dataset_ids"]:
                    raise ValueError("saved Pilot Dataset differs from its Campaign receipt")
                directory = (path.parent / "dataset_reviews" / campaign_id /
                             _key({"dataset_id": dataset_id}))
                review = review_dataset(agent, gateway, state_root, directory, request,
                                        dataset_id, [receipt], [], allow_optimization=False,
                                        execution_context=execution_context)
                if review["status"] not in {"no_selection", "optimization_deferred"}:
                    raise ValueError("saved Pilot Dataset review is unfinished")
                state.setdefault("dataset_reviews", {})[dataset_id] = review
            state.pop("error", None)
            if len(state["completed"]) >= state["budget"]["limits"]["batches"]:
                state["status"] = "scope_complete" if max_batches is None else "budget_complete"
            state["remaining_items"] = [
                item for index, item in enumerate(state["items"])
                if index not in {entry["item_index"] for entry in state["completed"]}]
            _checkpoint_campaign(state, state_root, path)
            if saved_pilot:
                write_campaign_summary(state, path.with_name(f"{campaign_id}_summary.json"))
            attempt_limit = state["budget"]["limits"]["batches"]
            iteration_ledger = IterationLedger(
                state_root, session_id=campaign_id, workflow="regular",
                objective=objective,
                scope={"region": region, "dataset_ids": dataset_ids,
                       "category": category, "delay": delay,
                       "universe": universe, "ppa_profile": bool(ppa_profile)},
                source_ref=str(path), max_iterations=attempt_limit)
            _sync_iteration_ledger(state, iteration_ledger)
            if not campaign_can_resume(state):
                terminal_status = (
                    "STOPPED" if state["status"] == "user_stopped" else
                    "BLOCKED" if state["status"] == "unresolved" else
                    "PARTIAL" if state["status"] == "partial" else "COMPLETED")
                iteration_ledger.finish(terminal_status, state.get("error") or state["status"])
            return {"campaign_id": campaign_id, "status": state["status"],
                    **({"saved_review_call_id": agent.call_id} if saved_pilot else {}),
                    "batches_completed": len(state["completed"]),
                    "budget": state["budget"], "completed": state["completed"],
                    "remaining_items": state["remaining_items"],
                    "subtasks": state["subtasks"],
                    "dataset_reviews": state.get("dataset_reviews", {}),
                    "unselected_dataset_ids": state["plan"].get("unselected_dataset_ids", []),
                    "unresolved": state["plan"]["unresolved"],
                    "state_path": str(path),
                    "iteration_session_id": campaign_id,
                    "iteration_ref": str(iteration_ledger.path),
                    "iterations_completed": iteration_ledger.record["budget"]["used"]}
    else:
        write_progress("正在核验 BRAIN 数据集与研究范围")
        gateway.call("set_case", case_key=campaign_id,
                     run_id=campaign_id)
        scope_options = gateway.call("get_platform_setting_options", include_schema=True)
        plan = discover_regular_scope(
            gateway, region=region, dataset_ids=dataset_ids,
            category=category, delay=delay, universe=universe, options=scope_options)
        items = _work_items(plan)
        plan["execution_order"] = items
        if max_batches is not None:
            plan["batches_per_dataset"] = max_batches
        plan["field_research"] = (execution_context is None and not ppa_profile
                                  and not ppa_requested(objective))
        evidence_path = path.with_name(f"{campaign_id}_scope_plan.json")
        atomic_write_json(evidence_path, plan)
        for dataset in plan["datasets"]:
            for combo in dataset["combinations"]:
                if "detail_scope" in combo and "scope_evidence_source" in combo:
                    fresh_dataset_proofs[(dataset["dataset_id"], combo["delay"], combo["universe"])] = {
                        **combo, "dataset_id": dataset["dataset_id"],
                        "requested_category": category, "plan_evidence_ref": str(evidence_path)}
        write_progress(f"范围核验完成，确认 {len(plan['datasets'])} 个可用数据集")
        if not items:
            write_progress("没有通过范围核验的研究组合")
            return {"status": "unresolved", "plan": plan,
                    "plan_evidence_ref": str(evidence_path), "reason": "no_verified_work_items"}
        attempt_limit = (len(items) if max_batches is None else
                         max_batches * len({item["dataset_id"] for item in items}))
        state = {"schema_version": 1, "campaign_id": campaign_id,
                 "plan_evidence_ref": str(evidence_path),
                 "request": request, "plan": plan, "items": items,
                 "completed": [], "active_item": None,
                 "budget": {"limits": {"batches": attempt_limit},
                            "reserved": {"batches": 0},
                            "used": {"batches": 0}},
                 "status": "planned", "created_at": _now(),
                 "updated_at": _now()}
        _checkpoint_campaign(state, state_root, path)
    items = state["items"]
    history = prior_campaign_findings(path.parent, campaign_id, items)
    items_by_dataset = {}
    for index, item in enumerate(items):
        items_by_dataset.setdefault(item["dataset_id"], []).append(index)
    dataset_slots = list(items_by_dataset.values())
    attempt_limit = state["budget"]["limits"]["batches"]
    iteration_ledger = IterationLedger(
        state_root, session_id=campaign_id, workflow="regular",
        objective=objective,
        scope={"region": region, "dataset_ids": dataset_ids,
               "category": category, "delay": delay,
               "universe": universe, "ppa_profile": bool(ppa_profile)},
        source_ref=str(path), max_iterations=attempt_limit)

    def sync_iteration_ledger():
        _sync_iteration_ledger(state, iteration_ledger)

    sync_iteration_ledger()
    if max_batches is None:
        schedule = list(range(attempt_limit))
    else:
        schedule = [slots[round_number % len(slots)]
                    for round_number in range(max_batches) for slots in dataset_slots]
        schedule = schedule[:attempt_limit]
    last_attempt = {items[index]["dataset_id"]: attempt
                    for attempt, index in enumerate(schedule, start=1)}
    emit_event("regular.campaign_scheduled", {
        "campaign_id": campaign_id, "dataset_ids": sorted(items_by_dataset),
        "work_item_count": len(items), "scheduled_batch_count": len(schedule),
        "batch_limit": attempt_limit,
        "plan_evidence_ref": str(path.with_name(f"{campaign_id}_scope_plan.json")),
    })
    reviewed_this_run = set()

    def finish_ready_datasets():
        reviews = state.setdefault("dataset_reviews", {})
        for dataset_id, last in last_attempt.items():
            if last > state["budget"]["used"]["batches"] or dataset_id in reviewed_this_run:
                continue
            if agent.stop_flag and agent.stop_flag.exists():
                return
            receipts = [row for row in state["completed"] if row["dataset_id"] == dataset_id]
            if not any(_needs_dataset_review(row, reviews) for row in receipts):
                continue
            reviewed_this_run.add(dataset_id)
            directory = (path.parent / "dataset_reviews" / campaign_id /
                         _key({"dataset_id": dataset_id}))
            try:
                reviews[dataset_id] = review_dataset(
                    agent, gateway, state_root, directory, request, dataset_id, receipts,
                    [failure for failure in state.get("failures", [])
                     if items[failure["item_index"]]["dataset_id"] == dataset_id],
                    allow_optimization=allow_optimization,
                    execution_context=execution_context)
                emit_event("regular.dataset_review_completed", {
                    "campaign_id": campaign_id, "dataset_id": dataset_id,
                    "status": reviews[dataset_id].get("status"),
                    "review_ref": (str(directory / "review.json")
                                   if (directory / "review.json").is_file() else None),
                })
                if reviews[dataset_id]["status"] == "attribution_pending":
                    write_progress(f"{dataset_id} · 数据集归因待补")
            except Exception as exc:
                saved = read_json(directory / "review.json", default={})
                reviews[dataset_id] = {
                    **saved, "status": saved.get("status", "attribution_pending"),
                    "error": str(exc)}
                emit_event("regular.dataset_review_blocked", {
                    "campaign_id": campaign_id, "dataset_id": dataset_id,
                    "exception_type": type(exc).__name__,
                    "review_ref": str(directory / "review.json"),
                })
                write_progress(f"{dataset_id} · 数据集收尾待恢复 · {type(exc).__name__} · {exc}")
            _checkpoint_campaign(state, state_root, path)

    while state["budget"]["used"]["batches"] < attempt_limit:
        finish_ready_datasets()
        if agent.stop_flag and agent.stop_flag.exists():
            state["status"] = "user_stopped"
            break
        used = state["budget"]["used"]["batches"]
        if resume_batch is not None:
            index = state["active_item"]
        else:
            index = schedule[used]
        item = items[index]
        write_progress(f"开始执行第 {used + 1}/{attempt_limit} 批 · {item['dataset_id']}")
        state["active_item"] = index
        if resume_batch is None:
            state["active_started_at"] = _now()
        state["budget"]["reserved"]["batches"] = 1
        state["status"] = "running"
        _checkpoint_campaign(state, state_root, path)
        emit_event("regular.batch_started", {
            "campaign_id": campaign_id, "item_index": index,
            "dataset_id": item["dataset_id"], "batch_number": used + 1,
            "batch_limit": attempt_limit, "resume": resume_batch is not None,
        })
        try:
            run_kwargs = {
                "fixed_scope": {"region": region, "delay": item["delay"],
                                "universe": item["universe"]},
                "ppa_profile": ppa_profile,
                "research_id": research_id,
                "field_pool_path": path.parent / "field_pools" / campaign_id / f"{_key(item)}.json",
                "expected_category": category,
            }
            if execution_context is not None:
                run_kwargs["execution_context"] = execution_context
                allow_diagnostic_simulations = execution_context.get(
                    "allow_diagnostic_simulations", True)
                if type(allow_diagnostic_simulations) is not bool:
                    raise ValueError("diagnostic Simulation policy must be boolean")
                run_kwargs["allow_diagnostic_simulations"] = allow_diagnostic_simulations
            if state["plan"].get("field_research"):
                run_kwargs["field_research"] = True
            if resume_batch is None:
                if scope_options is None:
                    scope_options = gateway.call("get_platform_setting_options", include_schema=True)
                if isinstance(scope_options, dict):
                    run_kwargs["scope_options"] = scope_options
                proof = fresh_dataset_proofs.get((item["dataset_id"], item["delay"], item["universe"]))
                if proof is not None:
                    run_kwargs["dataset_scope_proof"] = proof
                prior = (current_campaign_findings(state, item, limit=3) +
                         history[(item["dataset_id"], item["region"], item["delay"], item["universe"])])[:3]
                if prior:
                    run_kwargs["prior_research"] = prior
                result = agent.run_once(objective, dataset_id=item["dataset_id"], **run_kwargs)
            else:
                saved_execution = any((resume_batch / name).exists() for name in
                                      ("field_research_plan.json", "simulation_intent.json"))
                write_progress("正在恢复已保存的设计和回测进度" if saved_execution else
                               "核验并复用已完成的字段诊断回测")
                result = agent.recover(resume_batch, completed_probes_only=not saved_execution,
                                       execution_context=execution_context)
                resume_batch = None
            batch_dir = result.get("batch_dir") if isinstance(result, dict) else None
            if isinstance(result, dict) and result.get("field_research_status") == "user_stopped":
                state["status"] = "user_stopped"
                break
            if isinstance(result, dict) and result.get("field_research_status") == "unresolved":
                raise ValueError(f"no field expression completed; see {Path(batch_dir) / 'results.json'}")
            if (not isinstance(batch_dir, (str, Path)) or not str(batch_dir).strip()
                    or not isinstance(result.get("alpha_ids"), list) or not result["alpha_ids"]
                    or any(not isinstance(value, str) or not value.strip() for value in result["alpha_ids"])
                    or not isinstance(result.get("candidate_status"), (dict, str))):
                raise ValueError("batch returned an incomplete receipt")
            receipt = {
                "item_index": index, "dataset_id": item["dataset_id"],
                "batch_dir": str(batch_dir), "alpha_ids": result["alpha_ids"],
                "candidate_status": result["candidate_status"], "completed_at": _now(),
            }
            if result.get("field_research_status"):
                receipt["field_research_status"] = result["field_research_status"]
            if result.get("attribution_status"):
                receipt["attribution_status"] = result["attribution_status"]
        except Exception as exc:
            if agent.stop_flag and agent.stop_flag.exists():
                state["status"] = "user_stopped"
                break
            if not (execution_context and attempt_limit == 1):
                batch_ref = resume_batch or getattr(getattr(agent, "recorder", None), "batch_dir", None)
                _record_attempt_failure(state, str(exc), batch_ref)
                resume_batch = None
                _checkpoint_campaign(state, state_root, path)
                sync_iteration_ledger()
                emit_event("regular.batch_failed", {
                    "campaign_id": campaign_id, "item_index": index,
                    "dataset_id": item["dataset_id"],
                    "exception_type": type(exc).__name__,
                    "batch_ref": str(batch_ref) if batch_ref else None,
                })
                write_progress(f"第 {used + 1} 批未完成，已记录失败 · {type(exc).__name__}")
                continue
            state["status"] = "unresolved"
            state["error"] = str(exc)
            state["updated_at"] = _now()
            state.setdefault("failures", []).append({
                "item_index": index, "reason": state["error"],
                "recorded_at": state["updated_at"]})
            _checkpoint_campaign(state, state_root, path)
            emit_event("regular.batch_blocked", {
                "campaign_id": campaign_id, "item_index": index,
                "dataset_id": item["dataset_id"],
                "exception_type": type(exc).__name__,
                "batch_ref": str(getattr(getattr(agent, "recorder", None), "batch_dir", "")) or None,
            })
            raise
        state["completed"].append(receipt)
        write_progress(f"已完成 {len(state['completed'])}/{attempt_limit} 批")
        state["active_item"] = None
        state.pop("active_started_at", None)
        state["budget"]["reserved"]["batches"] = 0
        state["budget"]["used"]["batches"] += 1
        state.pop("error", None)
        state["status"] = "active"
        _checkpoint_campaign(state, state_root, path)
        sync_iteration_ledger()
        emit_event("regular.batch_completed", {
            "campaign_id": campaign_id, "item_index": index,
            "dataset_id": item["dataset_id"],
            "batch_ref": str(batch_dir), "alpha_ids": result["alpha_ids"],
            "candidate_status": result["candidate_status"],
            "completed_batch_count": len(state["completed"]),
            "budget_used": state["budget"]["used"]["batches"],
        })
    else:
        finish_ready_datasets()
        if agent.stop_flag and agent.stop_flag.exists():
            state["status"] = "user_stopped"
        elif not state["completed"]:
            state["status"] = "unresolved"
        elif (any(review["status"] not in {"complete", "no_selection"}
                  and (allow_optimization or review["status"] != "optimization_deferred")
                  for review in state.get("dataset_reviews", {}).values())
              or state["budget"]["used"]["batches"] > len(state["completed"])
              or any(entry.get("field_research_status") == "partial"
                     or entry.get("attribution_status") == "unresolved"
                     for entry in state["completed"])):
            state["status"] = "partial"
        else:
            state["status"] = "scope_complete" if max_batches is None else "budget_complete"
    state["remaining_items"] = [
        item for index, item in enumerate(items)
        if index not in {entry["item_index"] for entry in state["completed"]}]
    _checkpoint_campaign(state, state_root, path)
    sync_iteration_ledger()
    if not campaign_can_resume(state):
        terminal_status = (
            "STOPPED" if state["status"] == "user_stopped" else
            "BLOCKED" if state["status"] == "unresolved" else
            "PARTIAL" if state["status"] == "partial" else "COMPLETED")
        iteration_ledger.finish(terminal_status, state.get("error") or state["status"])
    summary_path = path.with_name(f"{campaign_id}_summary.json")
    write_campaign_summary(state, summary_path)
    write_progress(f"Campaign 复盘已保存 · {summary_path}")
    emit_event("regular.campaign_completed", {
        "campaign_id": campaign_id, "status": state["status"],
        "completed_batch_count": len(state["completed"]),
        "remaining_work_item_count": len(state["remaining_items"]),
        "dataset_review_statuses": {
            key: value.get("status") for key, value in
            state.get("dataset_reviews", {}).items()},
        "campaign_state_ref": str(path), "campaign_summary_ref": str(summary_path),
    })
    return {"campaign_id": campaign_id, "status": state["status"],
            "batches_completed": len(state["completed"]),
            "budget": state["budget"],
            "completed": state["completed"],
            "failures": state.get("failures", []),
            "remaining_items": state["remaining_items"],
            "subtasks": state["subtasks"],
            "dataset_reviews": state.get("dataset_reviews", {}),
            "unselected_dataset_ids": state["plan"].get("unselected_dataset_ids", []),
            "unresolved": state["plan"]["unresolved"],
            "state_path": str(path), "summary_path": str(summary_path),
            "iteration_session_id": campaign_id,
            "iteration_ref": str(iteration_ledger.path),
            "iterations_completed": iteration_ledger.record["budget"]["used"]}
