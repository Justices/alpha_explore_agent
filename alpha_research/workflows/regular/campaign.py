"""Bounded, serial execution of a discovered Regular Campaign."""

import hashlib
import json
from datetime import datetime, timezone, timedelta
from itertools import zip_longest
from pathlib import Path

from alpha_research.workflows.regular.scope import discover_regular_scope, regular_delay
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
            status = "completed"
        elif index in failures:
            status = "unresolved"
            group["status"] = "unresolved"
        elif state["status"] in {"budget_complete", "partial", "unresolved"}:
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
        if group["status"] != "eligible":
            continue
        if all(item["status"] == "completed" for item in group["combinations"]):
            group["status"] = "covered"
        elif state["status"] in {"budget_complete", "partial", "unresolved"}:
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
            ("budget_complete", "partial", "user_stopped", "unresolved") else None),
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


def run_regular_campaign(agent, gateway, *, objective, region, dataset_ids=None,
                         category=None, delay=None, universe=None,
                         max_batches, ppa_profile=False, execution_context=None,
                         recovery_batch=None):
    """Execute one batch per eligible item, then cycle while budget remains.

    Persist progress before and after each batch. An interrupted batch is
    unresolved until the existing recovery command has reconciled it.
    """
    if type(max_batches) is not int or max_batches < 1:
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
    research_id = f"regular:campaign:{campaign_id}"
    state_root = Path(agent.runs_root).resolve().parent
    path = (state_root / "state" /
            "research_v1_1" / "campaign_runs" / f"{campaign_id}.json")
    if recovery_batch is not None:
        recovery_batch = Path(recovery_batch).resolve()
    if recovery_batch is not None and not path.exists():
        raise ValueError("Campaign state is missing; recovery cannot reconstruct it safely")
    resume_probes = None
    scope_options = None
    fresh_dataset_proofs = {}
    if path.exists():
        state = json.loads(path.read_text(encoding="utf-8"))
        if "plan_evidence_ref" not in state:
            evidence_path = path.with_name(f"{campaign_id}_scope_plan.json")
            atomic_write_json(evidence_path, state["plan"])
            state["plan_evidence_ref"] = str(evidence_path)
        if state.get("request") != request:
            raise ValueError("saved Campaign request does not match")
        active_before = state.get("active_item")
        if active_before is not None:
            if "failures" not in state and state.get("error"):
                state["failures"] = [{"item_index": active_before,
                                      "reason": state["error"],
                                      "recorded_at": state["updated_at"]}]
            try:
                resume_probes = _reconcile_active(agent, state, recovery_batch)
            except ValueError as exc:
                if recovery_batch is not None or (execution_context and state["budget"]["limits"]["batches"] == 1):
                    raise
                _record_attempt_failure(state, str(exc))
                write_progress("先前批次需要单独恢复，继续处理剩余预算")
            _checkpoint_campaign(state, state_root, path)
        if recovery_batch is not None:
            if not any(Path(entry["batch_dir"]).resolve() == recovery_batch
                       for entry in state["completed"]):
                raise ValueError("bound recovery batch is missing from Campaign receipts")
            state.pop("error", None)
            if len(state["completed"]) >= state["budget"]["limits"]["batches"]:
                state["status"] = "budget_complete"
            state["remaining_items"] = [
                item for index, item in enumerate(state["items"])
                if index not in {entry["item_index"] for entry in state["completed"]}]
            _checkpoint_campaign(state, state_root, path)
            return {"campaign_id": campaign_id, "status": state["status"],
                    "batches_completed": len(state["completed"]),
                    "budget": state["budget"], "completed": state["completed"],
                    "remaining_items": state["remaining_items"],
                    "subtasks": state["subtasks"],
                    "unselected_dataset_ids": state["plan"].get("unselected_dataset_ids", []),
                    "unresolved": state["plan"]["unresolved"],
                    "state_path": str(path)}
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
        plan["batches_per_dataset"] = max_batches
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
        state = {"schema_version": 1, "campaign_id": campaign_id,
                 "plan_evidence_ref": str(evidence_path),
                 "request": request, "plan": plan, "items": items,
                 "completed": [], "active_item": None,
                 "budget": {"limits": {"batches": max_batches * len({item["dataset_id"] for item in items})},
                            "reserved": {"batches": 0},
                            "used": {"batches": 0}},
                 "status": "planned", "created_at": _now(),
                 "updated_at": _now()}
        _checkpoint_campaign(state, state_root, path)
    items = state["items"]
    items_by_dataset = {}
    for index, item in enumerate(items):
        items_by_dataset.setdefault(item["dataset_id"], []).append(index)
    dataset_slots = list(items_by_dataset.values())
    attempt_limit = state["budget"]["limits"]["batches"]
    while state["budget"]["used"]["batches"] < attempt_limit:
        if agent.stop_flag and agent.stop_flag.exists():
            state["status"] = "user_stopped"
            break
        used = state["budget"]["used"]["batches"]
        round_number, dataset_slot = divmod(used, len(dataset_slots))
        combinations = dataset_slots[dataset_slot]
        index = (state["active_item"] if resume_probes is not None
                 else combinations[round_number % len(combinations)])
        item = items[index]
        write_progress(f"开始执行第 {used + 1}/{attempt_limit} 批 · {item['dataset_id']}")
        state["active_item"] = index
        if resume_probes is None:
            state["active_started_at"] = _now()
        state["budget"]["reserved"]["batches"] = 1
        state["status"] = "running"
        _checkpoint_campaign(state, state_root, path)
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
            if resume_probes is None:
                if scope_options is None:
                    scope_options = gateway.call("get_platform_setting_options", include_schema=True)
                if isinstance(scope_options, dict):
                    run_kwargs["scope_options"] = scope_options
                proof = fresh_dataset_proofs.get((item["dataset_id"], item["delay"], item["universe"]))
                if proof is not None:
                    run_kwargs["dataset_scope_proof"] = proof
                result = agent.run_once(objective, dataset_id=item["dataset_id"], **run_kwargs)
            else:
                write_progress("核验并复用已完成的字段诊断回测")
                result = agent.recover(resume_probes, completed_probes_only=True,
                                       execution_context=execution_context)
                resume_probes = None
            batch_dir = result.get("batch_dir") if isinstance(result, dict) else None
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
        except Exception as exc:
            if not (execution_context and attempt_limit == 1):
                batch_ref = resume_probes or getattr(getattr(agent, "recorder", None), "batch_dir", None)
                _record_attempt_failure(state, str(exc), batch_ref)
                resume_probes = None
                _checkpoint_campaign(state, state_root, path)
                write_progress(f"第 {used + 1} 批未完成，已记录失败 · {type(exc).__name__}")
                continue
            state["status"] = "unresolved"
            state["error"] = str(exc)
            state["updated_at"] = _now()
            state.setdefault("failures", []).append({
                "item_index": index, "reason": state["error"],
                "recorded_at": state["updated_at"]})
            _checkpoint_campaign(state, state_root, path)
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
    else:
        if agent.stop_flag and agent.stop_flag.exists():
            state["status"] = "user_stopped"
        elif not state["completed"]:
            state["status"] = "unresolved"
        elif state["budget"]["used"]["batches"] > len(state["completed"]):
            state["status"] = "partial"
        else:
            state["status"] = "budget_complete"
    state["remaining_items"] = [
        item for index, item in enumerate(items)
        if index not in {entry["item_index"] for entry in state["completed"]}]
    _checkpoint_campaign(state, state_root, path)
    return {"campaign_id": campaign_id, "status": state["status"],
            "batches_completed": len(state["completed"]),
            "budget": state["budget"],
            "completed": state["completed"],
            "failures": state.get("failures", []),
            "remaining_items": state["remaining_items"],
            "subtasks": state["subtasks"],
            "unselected_dataset_ids": state["plan"].get("unselected_dataset_ids", []),
            "unresolved": state["plan"]["unresolved"],
            "state_path": str(path)}
