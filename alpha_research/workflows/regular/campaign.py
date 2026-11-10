"""Bounded, serial execution of a discovered Regular Campaign."""

import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from random import choice

from alpha_research.workflows.regular.scope import discover_regular_scope
from alpha_research.state.store import atomic_write_json
from alpha_research.state.records import ResearchRecordStore


def _now():
    return datetime.now(timezone.utc).isoformat()


def _key(request):
    raw = json.dumps(request, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _work_items(plan):
    return [{"dataset_id": dataset["dataset_id"],
             "category": dataset["category"],
             "region": combo["region"],
             "delay": combo["delay"],
             "universe": combo["universe"]}
            for dataset in plan["datasets"]
            for combo in dataset["combinations"]]


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
        elif state["status"] == "budget_complete":
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
        elif state["status"] == "budget_complete":
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


def _sync_subtasks(state, state_root):
    """Persist Campaign -> Dataset -> Combination lineage and coverage."""
    store = ResearchRecordStore(state_root)
    summaries = _subtask_summary(state)
    completed = {entry["item_index"] for entry in state["completed"]}
    subtask_ids = []
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
            _persist_record(store, "combination", {
                "schema_version": 1, "combination_id": combination_id,
                "subtask_id": subtask_id, "effective_settings": effective,
                "support_evidence_ref": support_ref, "status": combination["status"],
                "failure_history": combination["failure_history"],
                "batch_refs": [entry["batch_dir"] for entry in receipts],
                "candidate_statuses": [
                    entry["candidate_status"] for entry in receipts],
                "completed_batches": len(receipts)})
        summary["combination_ids"] = combination_ids
        _persist_record(store, "dataset_subtask", {
            "schema_version": 1, "subtask_id": subtask_id,
            "campaign_id": state["campaign_id"],
            "dataset_id": summary["dataset_id"],
            "combination_ids": combination_ids,
            "combinations": summary["combinations"],
            "completed_batches": summary["completed_batches"],
            "status": summary["status"]})
    state["subtasks"] = summaries
    request = state["request"]
    _persist_record(store, "campaign", {
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
            ("budget_complete", "user_stopped", "unresolved") else None),
        "coverage": {
            "eligible_combinations": len(state["items"]),
            "completed_combinations": len(completed),
            "unexamined_combinations": len(state["items"]) - len(completed),
            "unselected_dataset_ids":
                state["plan"].get("unselected_dataset_ids", []),
            "unresolved": state["plan"].get("unresolved", [])}})


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
    for context_path in context_paths:
        try:
            context = json.loads(context_path.read_text(encoding="utf-8"))
            started = datetime.fromisoformat(context.get("started_at", ""))
            active = datetime.fromisoformat(state["active_started_at"])
            if (context.get("objective") == state["request"]["objective"]
                    and started >= active):
                matches.append(context_path.parent)
        except (OSError, ValueError):
            raise ValueError("interrupted Campaign context is unreadable")
    if len(matches) > 1:
        raise ValueError("multiple possible interrupted batches; manual reconciliation required")
    if not matches:
        if recovery_batch is not None:
            raise ValueError("bound recovery batch does not match the active Campaign item")
        # run_once failed before recording a batch context, hence before a POST.
        state["active_item"] = None
        state.pop("active_started_at", None)
        state["budget"]["reserved"]["batches"] = 0
        state["status"] = "planned"
        return
    batch_dir = matches[0]
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
    state["budget"]["used"]["batches"] = len(state["completed"])
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
    request = {"objective": objective, "region": region,
               "dataset_ids": dataset_ids, "category": category,
               "delay": delay, "universe": universe,
               "max_batches": max_batches, "ppa_profile": ppa_profile}
    campaign_id = _key(request)
    research_id = f"regular:campaign:{campaign_id}"
    state_root = Path(agent.runs_root).resolve().parent
    path = (state_root / "state" /
            "research_v1_1" / "campaign_runs" / f"{campaign_id}.json")
    if recovery_batch is not None:
        recovery_batch = Path(recovery_batch).resolve()
    if recovery_batch is not None and not path.exists():
        raise ValueError("Campaign state is missing; recovery cannot reconstruct it safely")
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
            _reconcile_active(agent, state, recovery_batch)
            state["updated_at"] = _now()
            _sync_subtasks(state, state_root)
            atomic_write_json(path, state)
        if recovery_batch is not None:
            if not any(Path(entry["batch_dir"]).resolve() == recovery_batch
                       for entry in state["completed"]):
                raise ValueError("bound recovery batch is missing from Campaign receipts")
            state.pop("error", None)
            if len(state["completed"]) >= max_batches:
                state["status"] = "budget_complete"
            state["remaining_items"] = [
                item for index, item in enumerate(state["items"])
                if index not in {entry["item_index"] for entry in state["completed"]}]
            state["updated_at"] = _now()
            _sync_subtasks(state, state_root)
            atomic_write_json(path, state)
            return {"campaign_id": campaign_id, "status": state["status"],
                    "batches_completed": len(state["completed"]),
                    "budget": state["budget"], "completed": state["completed"],
                    "remaining_items": state["remaining_items"],
                    "subtasks": state["subtasks"],
                    "unselected_dataset_ids": state["plan"].get("unselected_dataset_ids", []),
                    "unresolved": state["plan"]["unresolved"],
                    "state_path": str(path)}
    else:
        gateway.call("set_case", case_key=campaign_id,
                     run_id=campaign_id)
        plan = discover_regular_scope(
            gateway, region=region, dataset_ids=dataset_ids,
            category=category, delay=delay, universe=universe)
        if dataset_ids is None and plan["datasets"]:
            chosen = choice(plan["datasets"])
            plan["agent_selection"] = [{
                "dataset_id": chosen["dataset_id"], "mode": "random",
                "reason": "Dataset omitted; uniformly sampled from verified eligible Datasets"}]
            plan["unselected_dataset_ids"] = [
                item["dataset_id"] for item in plan["datasets"]
                if item["dataset_id"] != chosen["dataset_id"]]
            items = _work_items({"datasets": [chosen]})
        else:
            items = _work_items(plan)
        evidence_path = path.with_name(f"{campaign_id}_scope_plan.json")
        atomic_write_json(evidence_path, plan)
        if not items:
            return {"status": "unresolved", "plan": plan,
                    "plan_evidence_ref": str(evidence_path), "reason": "no_verified_work_items"}
        state = {"schema_version": 1, "campaign_id": campaign_id,
                 "plan_evidence_ref": str(evidence_path),
                 "request": request, "plan": plan, "items": items,
                 "completed": [], "active_item": None,
                 "budget": {"limits": {"batches": max_batches},
                            "reserved": {"batches": 0},
                            "used": {"batches": 0}},
                 "status": "planned", "created_at": _now(),
                 "updated_at": _now()}
        _sync_subtasks(state, state_root)
        atomic_write_json(path, state)
    items = state["items"]
    while len(state["completed"]) < max_batches:
        if agent.stop_flag and agent.stop_flag.exists():
            state["status"] = "user_stopped"
            break
        index = len(state["completed"]) % len(items)
        item = items[index]
        state["active_item"] = index
        state["active_started_at"] = _now()
        state["budget"]["reserved"]["batches"] = 1
        state["status"] = "running"
        _sync_subtasks(state, state_root)
        state["updated_at"] = _now()
        atomic_write_json(path, state)
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
            result = agent.run_once(
                objective, dataset_id=item["dataset_id"], **run_kwargs)
        except Exception as exc:
            state["status"] = "unresolved"
            state["error"] = str(exc)
            state["updated_at"] = _now()
            state.setdefault("failures", []).append({
                "item_index": index, "reason": state["error"],
                "recorded_at": state["updated_at"]})
            # Keep the recovery checkpoint even if a derived record cannot be written.
            atomic_write_json(path, state)
            _sync_subtasks(state, state_root)
            atomic_write_json(path, state)
            raise
        state["completed"].append({
            "item_index": index, "dataset_id": item["dataset_id"],
            "batch_dir": result["batch_dir"],
            "alpha_ids": result["alpha_ids"],
            "candidate_status": result["candidate_status"],
            "completed_at": _now()})
        state["active_item"] = None
        state.pop("active_started_at", None)
        state["budget"]["reserved"]["batches"] = 0
        state["budget"]["used"]["batches"] = len(state["completed"])
        state.pop("error", None)
        state["status"] = "active"
        _sync_subtasks(state, state_root)
        state["updated_at"] = _now()
        atomic_write_json(path, state)
    else:
        state["status"] = "budget_complete"
    state["remaining_items"] = [
        item for index, item in enumerate(items)
        if index not in {entry["item_index"] for entry in state["completed"]}]
    state["updated_at"] = _now()
    _sync_subtasks(state, state_root)
    atomic_write_json(path, state)
    return {"campaign_id": campaign_id, "status": state["status"],
            "batches_completed": len(state["completed"]),
            "budget": state["budget"],
            "completed": state["completed"],
            "remaining_items": state["remaining_items"],
            "subtasks": state["subtasks"],
            "unselected_dataset_ids": state["plan"].get("unselected_dataset_ids", []),
            "unresolved": state["plan"]["unresolved"],
            "state_path": str(path)}
