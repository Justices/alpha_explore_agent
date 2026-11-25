"""Read-only execution trace projection across Task, domain and model evidence."""
import hashlib
import json
import re
from pathlib import Path

from alpha_research.orchestration.event_journal import ExecutionEventJournal
from alpha_research.orchestration.engine import MainOrchestrator
from alpha_research.state.records import ResearchRecordStore
from alpha_research.state.sealed import is_sealed_state


def _collect_values(value, target_key):
    found = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == target_key and isinstance(child, str) and child:
                found.add(child)
            found.update(_collect_values(child, target_key))
    elif isinstance(value, list):
        for child in value:
            found.update(_collect_values(child, target_key))
    return found


def _collect_references(value):
    refs = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key.endswith("_ref") and isinstance(child, str):
                refs.add(child)
            refs.update(_collect_references(child))
    elif isinstance(value, list):
        for child in value:
            refs.update(_collect_references(child))
    return refs


def _read_object(path, *, root=None):
    try:
        path = Path(path)
        if root is not None:
            root = Path(root).resolve()
            if not path.is_absolute():
                path = root / path
            if path.is_symlink():
                return None
            path = path.resolve(strict=True)
            if not path.is_relative_to(root):
                return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError):
        return None
    return value if isinstance(value, dict) else None


def _evidence_manifest(root, references):
    """Hash referenced files and bounded batch artifacts without exposing contents."""
    root = Path(root).resolve()
    rows = []
    missing = []
    seen = set()
    total_bytes = 0
    truncated = False
    allowed_batch_bytes = 256 * 1024 * 1024
    for reference in sorted(value for value in references
                             if isinstance(value, str) and value):
        if reference.startswith("session:"):
            continue
        candidate = Path(reference)
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            if candidate.is_symlink():
                missing.append({"reference": reference, "reason": "symlink_refused"})
                continue
            path = candidate.resolve(strict=True)
            if not path.is_relative_to(root):
                missing.append({"reference": reference, "reason": "outside_state_root"})
                continue
        except (OSError, RuntimeError):
            missing.append({"reference": reference, "reason": "missing_or_unreadable"})
            continue
        targets = []
        if path.is_file():
            targets = [path]
        elif path.is_dir() and path.is_relative_to(root / "runs"):
            for child in sorted(path.rglob("*")):
                if child.is_symlink() or not child.is_file():
                    continue
                try:
                    resolved = child.resolve(strict=True)
                    if resolved.is_relative_to(root):
                        targets.append(resolved)
                except (OSError, RuntimeError):
                    continue
                if len(targets) >= 500:
                    truncated = True
                    break
        for target in targets:
            key = str(target)
            if key in seen:
                continue
            try:
                size = target.stat().st_size
                if total_bytes + size > allowed_batch_bytes:
                    truncated = True
                    break
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
            except OSError:
                missing.append({"reference": str(target), "reason": "unreadable_file"})
                continue
            seen.add(key)
            total_bytes += size
            rows.append({"path": key, "name": target.name,
                         "size_bytes": size, "sha256": digest})
    return {"status": "COMPLETE" if not missing and not truncated else "PARTIAL",
            "files": rows, "file_count": len(rows),
            "missing_references": missing,
            "total_bytes": total_bytes, "truncated": truncated}


def _safe_model_call(path, record):
    allowed = (
        "call_id", "ts", "task", "agent_role", "agent_backend", "model",
        "session_id", "research_id", "batch_ref", "status",
        "context_sha256", "route_sha256", "response_sha256",
        "context_ref", "response_ref", "record_ref", "knowledge_pack_ref",
        "token_in", "token_out", "latency_ms", "provider",
    )
    row = {key: record[key] for key in allowed if key in record}
    row["record_path"] = str(path)
    row["record_ref"] = str(path)
    return row


def _campaign_batch_intents(root, completed):
    rows = []
    for receipt in completed:
        if not isinstance(receipt, dict) or not isinstance(receipt.get("batch_dir"), str):
            continue
        parent = Path(receipt["batch_dir"])
        parent_results = _read_object(parent / "results.json", root=root) or {}
        children = parent_results.get("child_batches")
        targets = (children if isinstance(children, list) and children else
                   [{"batch_dir": str(parent), "alpha_ids": receipt.get("alpha_ids", [])}])
        for child in targets:
            if not isinstance(child, dict) or not isinstance(child.get("batch_dir"), str):
                continue
            batch = Path(child["batch_dir"])
            intent_ref = str(batch / "simulation_intent.json")
            intent = _read_object(batch / "simulation_intent.json", root=root)
            rows.append({
                "dataset_id": receipt.get("dataset_id"),
                "alpha_ids": child.get("alpha_ids"),
                "batch_ref": str(batch),
                "intent_ref": intent_ref,
                "intent_status": intent.get("status") if intent else None,
            })
    return rows


def _model_budget_trace(root, research_id, has_calls):
    budget_stem = hashlib.sha256(research_id.encode("utf-8")).hexdigest()
    budget_path = (Path(root) / "state" / "logs" / "model_budget" /
                   f"{budget_stem}.json")
    result = {
        "research_id": research_id,
        "status": "NOT_CREATED",
        "ledger_ref": str(budget_path),
        "summary": None,
        "calls": [],
    }
    state = _read_object(budget_path, root=root)
    if state is None:
        if has_calls:
            result["status"] = "MISSING"
        return result
    rows = state.get("calls")
    if (state.get("research_id") != research_id
            or not is_sealed_state(state)
            or not isinstance(rows, list)
            or not all(isinstance(row, dict) for row in rows)):
        result["status"] = "INVALID"
        return result
    result["status"] = "VERIFIED"
    result["summary"] = {
        "attempts": len(rows),
        "successful": sum(row.get("status") == "OK" for row in rows),
        "failed": sum(row.get("status") == "ERROR" for row in rows),
        "unknown": sum(row.get("status") == "UNKNOWN" for row in rows),
        "unresolved": sum(row.get("status") == "IN_FLIGHT" for row in rows),
        "reported_tokens": sum(
            value for row in rows
            for value in (row.get("token_in"), row.get("token_out"))
            if type(value) is int),
    }
    result["calls"] = [{
        key: row.get(key) for key in (
            "call_id", "task", "role", "backend", "status",
            "token_in", "token_out", "backend_sha256")
        if key in row
    } for row in rows]
    return result


def inspect_execution_trace(state_root, task_id):
    """Verify the Task event chain and correlate existing model/domain records."""
    root = Path(state_root).resolve()
    task = MainOrchestrator(root).load(task_id)
    journal = ExecutionEventJournal(root, task_id)
    events = journal.read_events()
    verification = journal.verify()
    trace_meta = task.get("trace")
    trace_ref = trace_meta.get("journal_ref") if isinstance(trace_meta, dict) else None
    trace_binding_valid = trace_ref == str(journal.path)
    trace_events_consistent = len(events) == verification["event_count"]
    research_id = (task.get("research_identity") or {}).get("research_id")
    research_ids = list(dict.fromkeys(
        [value for value in [research_id] if isinstance(value, str)] +
        [event.get("research_id") for event in events
         if isinstance(event.get("research_id"), str)]))

    calls = []
    calls_root = root / "state" / "logs" / "model_calls"
    if calls_root.exists() and research_ids:
        for path in sorted(calls_root.glob("*.record.json")):
            record = _read_object(path, root=root)
            if record and record.get("research_id") in set(research_ids):
                calls.append(_safe_model_call(path, record))

    model_budgets = [
        _model_budget_trace(root, current_id, any(
            row.get("research_id") == current_id for row in calls))
        for current_id in research_ids
    ]
    model_budget = next(
        (row for row in model_budgets if row["research_id"] == research_id),
        model_budgets[0] if model_budgets else None)
    for budget_row in model_budgets:
        if budget_row.get("status") != "VERIFIED":
            continue
        expected_ids = {row.get("call_id") for row in budget_row.get("calls", [])
                        if isinstance(row.get("call_id"), str)}
        actual_ids = {row.get("call_id") for row in calls
                      if row.get("research_id") == budget_row["research_id"]
                      and isinstance(row.get("call_id"), str)}
        missing_ids = sorted(expected_ids - actual_ids)
        budget_row["model_record_coverage"] = {
            "status": "COMPLETE" if not missing_ids else "PARTIAL",
            "expected_call_count": len(expected_ids),
            "recorded_call_count": len(expected_ids & actual_ids),
            "missing_call_ids": missing_ids,
        }
    session_ids = sorted({row.get("session_id") for row in calls
                          if isinstance(row.get("session_id"), str)})
    sessions = []
    for session_id in session_ids:
        path = root / "state" / "logs" / "model_sessions" / f"{session_id}.json"
        index = _read_object(path, root=root)
        raw_call_ids = index.get("call_ids") if index else None
        expected_calls = set(raw_call_ids) if isinstance(raw_call_ids, list) else set()
        actual_calls = {row.get("call_id") for row in calls
                        if row.get("session_id") == session_id}
        sessions.append({
            "session_id": session_id,
            "status": "READABLE" if index else "MISSING_OR_UNREADABLE",
            "index_ref": str(path),
            "call_ids": sorted(expected_calls) if index else [],
            "missing_call_ids": sorted(expected_calls - actual_calls),
            "unindexed_call_ids": sorted(actual_calls - expected_calls),
            "batches": index.get("batches", []) if index else [],
        })

    domain = {"campaign": None, "super_run": None,
              "optimization": None, "optimization_runs": [],
              "simulation_intents": [],
              "qualification_records": {"status": "UNAVAILABLE",
                                        "candidates": [], "preferred": []}}
    if isinstance(research_id, str) and research_id.startswith("regular:campaign:"):
        campaign_id = research_id.removeprefix("regular:campaign:")
        path = root / "state" / "research_v1_1" / "campaign_runs" / f"{campaign_id}.json"
        saved = _read_object(path, root=root)
        if saved:
            domain["campaign"] = {
                "campaign_id": campaign_id, "status": saved.get("status"),
                "budget": saved.get("budget"),
                "completed": [{
                    "dataset_id": row.get("dataset_id"),
                    "alpha_ids": row.get("alpha_ids"),
                    "batch_ref": row.get("batch_dir"),
                    "completed_at": row.get("completed_at"),
                } for row in saved.get("completed", []) if isinstance(row, dict)],
                "dataset_reviews": {
                    key: {field: value.get(field) for field in (
                        "status", "source", "optimization_run_id")
                        if field in value}
                    for key, value in saved.get("dataset_reviews", {}).items()
                    if isinstance(value, dict)
                },
                "campaign_state_ref": str(path),
                "campaign_summary_ref": str(path.with_name(f"{campaign_id}_summary.json")),
                "batch_intents": _campaign_batch_intents(root, saved.get("completed", [])),
            }
    elif isinstance(research_id, str) and research_id.startswith("super:"):
        run_id = research_id.removeprefix("super:")
        path = root / "state" / "research_v1_1" / "super_runs" / f"{run_id}.json"
        saved = _read_object(path, root=root)
        if saved:
            domain["super_run"] = {
                key: saved.get(key) for key in (
                    "run_id", "status", "region", "objective", "batch_id",
                    "alpha_id", "candidate_id", "candidate_status",
                    "preferred_status", "candidate_record_ref",
                    "preferred_record_ref", "prereg_ref", "simulation_intent_ref",
                    "response_ref", "readback_ref", "audit_ref")
                if key in saved
            }
            domain["super_run"]["record_ref"] = str(path)
    elif isinstance(research_id, str) and research_id.startswith("optimization:"):
        run_id = research_id.removeprefix("optimization:")
        base = root / "state" / "research_v1_1" / "optimization"
        report_path = base / f"{run_id}_optimization_report.json"
        report = _read_object(report_path, root=root)
        branch_id = next((event.get("payload", {}).get("branch_id")
                          for event in events
                          if event.get("event_type") ==
                          "optimization.candidate_branch_created"), None)
        branch = None
        source = None
        store = ResearchRecordStore(root)
        if branch_id:
            try:
                branch = store.get("candidate_branch", branch_id)
                source = store.get("optimization_source", branch["source_id"])
            except (OSError, ValueError, KeyError):
                pass
        domain["optimization"] = {
            "run_id": run_id,
            "report_ref": str(report_path) if report else None,
            "report": ({key: report.get(key) for key in (
                "source_alpha_id", "candidate_alpha_id", "candidate_id",
                "candidate_status", "preferred_status", "dataset_id",
                "candidate_record_ref", "preferred_record_ref",
                "source_snapshot_ref", "simulation_intent_ref", "readback_ref",
                "audit_ref", "source_unchanged_verified")} if report else None),
            "branch": ({key: branch.get(key) for key in (
                "branch_id", "status", "source_id", "source_alpha_id",
                "source_snapshot_ref", "candidate_alpha_id", "candidate_id",
                "candidate_status", "preferred_status", "simulation_intent_ref",
                "receipt_ref", "audit_ref", "report_ref",
                "candidate_record_ref", "preferred_record_ref")} if branch else None),
            "source": ({key: source.get(key) for key in (
                "source_id", "alpha_id", "source_type", "snapshot_ref",
                "details_sha256", "source_properties_sha256")} if source else None),
        }

    for current_id in research_ids:
        if not current_id.startswith("optimization:"):
            continue
        run_id = current_id.removeprefix("optimization:")
        if any(row.get("run_id") == run_id for row in domain["optimization_runs"]):
            continue
        base = root / "state" / "research_v1_1" / "optimization"
        report_path = base / f"{run_id}_optimization_report.json"
        report = _read_object(report_path, root=root)
        branch_id = next((event.get("payload", {}).get("branch_id")
                          for event in events
                          if event.get("event_type") ==
                          "optimization.candidate_branch_created"
                          and event.get("payload", {}).get("run_id") == run_id), None)
        branch = None
        source = None
        if branch_id:
            try:
                record_store = ResearchRecordStore(root)
                branch = record_store.get("candidate_branch", branch_id)
                source = record_store.get("optimization_source", branch["source_id"])
            except (OSError, ValueError, KeyError):
                pass
        run_evidence = {
            "run_id": run_id,
            "report_ref": str(report_path) if report else None,
            "report": ({key: report.get(key) for key in (
                "source_alpha_id", "candidate_alpha_id", "candidate_id",
                "candidate_status", "preferred_status", "dataset_id",
                "candidate_record_ref", "preferred_record_ref",
                "source_snapshot_ref", "simulation_intent_ref", "readback_ref",
                "audit_ref", "source_unchanged_verified")} if report else None),
            "branch": ({key: branch.get(key) for key in (
                "branch_id", "status", "source_id", "source_alpha_id",
                "source_snapshot_ref", "candidate_alpha_id", "candidate_id",
                "candidate_status", "preferred_status", "simulation_intent_ref",
                "receipt_ref", "audit_ref", "report_ref",
                "candidate_record_ref", "preferred_record_ref")} if branch else None),
            "source": ({key: source.get(key) for key in (
                "source_id", "alpha_id", "source_type", "snapshot_ref",
                "details_sha256", "source_properties_sha256")} if source else None),
        }
        domain["optimization_runs"].append(run_evidence)
        if domain["optimization"] is None:
            domain["optimization"] = run_evidence

    candidate_ids = set()
    for event in events:
        candidate_ids.update(_collect_values(event.get("payload") or {}, "candidate_id"))
    if (domain.get("super_run") or {}).get("candidate_id"):
        candidate_ids.add((domain.get("super_run") or {})["candidate_id"])
    for optimization in domain.get("optimization_runs", []):
        candidate_id = (optimization.get("report") or {}).get("candidate_id")
        if isinstance(candidate_id, str):
            candidate_ids.add(candidate_id)
    candidate_ids = {value for value in candidate_ids
                     if re.fullmatch(r"[0-9a-f]{32}", value)}
    try:
        store = ResearchRecordStore(root)
        candidate_rows, preferred_rows = [], []
        for candidate_id in sorted(candidate_ids):
            try:
                candidate = store.get("candidate", candidate_id)
                if candidate:
                    candidate_rows.append(candidate)
            except (KeyError, FileNotFoundError):
                pass
            try:
                preferred = store.get("preferred", candidate_id)
                if preferred:
                    preferred_rows.append(preferred)
            except (KeyError, FileNotFoundError):
                pass
        def qualification_summary(row):
            return {key: row.get(key) for key in (
                "candidate_id", "alpha_id", "workflow", "status",
                "candidate_status", "preferred_status", "metrics",
                "failures", "pending", "source_ref", "candidate_ref",
                "evidence_refs", "created_at", "updated_at") if key in row}
        domain["qualification_records"] = {
            "status": "READABLE",
            "candidates": [qualification_summary(row) for row in candidate_rows],
            "preferred": [qualification_summary(row) for row in preferred_rows],
        }
    except (OSError, ValueError, KeyError, TypeError):
        domain["qualification_records"]["status"] = "UNREADABLE"

    intent_refs = set()
    intent_context = {}
    for event in events:
        payload = event.get("payload") or {}
        event_type = event.get("event_type", "")
        inferred_workflow = (event_type.split(".", 1)[0]
                             if event_type.startswith(("regular.", "super.", "optimization."))
                             else None)
        for key, value in payload.items():
            if key.endswith("_ref") and isinstance(value, str):
                intent_refs.add(value)
                if "intent" in key and inferred_workflow in {"regular", "super", "optimization"}:
                    intent_context[value] = {
                        "workflow": inferred_workflow,
                        "batch_ref": payload.get("batch_ref"),
                        "probe_batch": payload.get("probe_batch"),
                    }
    if domain.get("campaign"):
        for row in domain["campaign"].get("batch_intents", []):
            intent_ref = row.get("intent_ref")
            if isinstance(intent_ref, str):
                intent_refs.add(intent_ref)
                intent_context.setdefault(intent_ref, {
                    "workflow": "regular", "batch_ref": row.get("batch_ref")})
    recovery_target = task.get("recovery_target") or {}
    if recovery_target.get("kind") == "simulation_intent":
        intent_refs.add(recovery_target.get("ref"))
    for ref in sorted(value for value in intent_refs if value):
        path = Path(ref)
        if not path.is_absolute():
            path = root / path
        try:
            if path.is_symlink():
                continue
            path = path.resolve(strict=True)
            if not path.is_relative_to(root):
                continue
        except (OSError, RuntimeError):
            continue
        intent = _read_object(path, root=root)
        inferred = intent_context.get(ref, {})
        workflow = (intent.get("workflow") if intent else None) or inferred.get("workflow")
        if intent and workflow in ("regular", "super", "optimization"):
            body = {
                "workflow": workflow,
                "batch_ref": inferred.get("batch_ref"),
                "probe_batch": inferred.get("probe_batch"),
                "status": intent.get("status"),
                "run_id": intent.get("run_id"),
                "batch_id": intent.get("batch_id"),
                "response_sha256": intent.get("response_sha256"),
                "response_ref": intent.get("response_ref"),
                "receipt_ref": intent.get("receipt_ref"),
                "intent_ref": str(path),
            }
            body["intent_sha256"] = hashlib.sha256(
                path.read_bytes()).hexdigest()
            domain["simulation_intents"].append(body)

    evidence_refs = {
        str(root / "state" / "research_v1_1" / "orchestration" /
            f"{task_id}.json")
    }
    if isinstance(trace_ref, str):
        evidence_refs.add(trace_ref)
    for event in events:
        evidence_refs.update(_collect_references(event.get("payload") or {}))
    for budget_row in model_budgets:
        if budget_row.get("ledger_ref") and Path(budget_row["ledger_ref"]).is_file():
            evidence_refs.add(budget_row["ledger_ref"])
    for row in calls:
        evidence_refs.update(row.get(key) for key in (
            "context_ref", "response_ref", "record_ref",
            "knowledge_pack_ref", "batch_ref") if isinstance(row.get(key), str))
    for row in sessions:
        evidence_refs.add(row["index_ref"])
    if domain.get("campaign"):
        evidence_refs.add(domain["campaign"]["campaign_state_ref"])
        evidence_refs.add(domain["campaign"]["campaign_summary_ref"])
        for row in domain["campaign"].get("batch_intents", []):
            evidence_refs.update(row.get(key) for key in ("batch_ref", "intent_ref")
                                 if isinstance(row.get(key), str))
        for row in domain["campaign"].get("dataset_reviews", {}).values():
            if isinstance(row.get("review_ref"), str):
                evidence_refs.add(row["review_ref"])
    if domain.get("super_run"):
        evidence_refs.update(value for key, value in
                             domain["super_run"].items()
                             if key.endswith("_ref") and isinstance(value, str))
    for optimization in domain.get("optimization_runs", []):
        if isinstance(optimization.get("report_ref"), str):
            evidence_refs.add(optimization["report_ref"])
        for group in ("report", "branch", "source"):
            evidence_refs.update(
                value for key, value in (optimization.get(group) or {}).items()
                if key.endswith("_ref") and isinstance(value, str))
    target_ref = (task.get("recovery_target") or {}).get("ref")
    if isinstance(target_ref, str):
        evidence_refs.add(target_ref)
    evidence_manifest = _evidence_manifest(root, evidence_refs)

    return {
        "schema_version": 1,
        "status": ("TRACE_VERIFIED" if verification["status"] == "VERIFIED"
                   and verification["event_count"] > 0
                   and trace_binding_valid and trace_events_consistent
                   else "TRACE_INCOMPLETE"),
        "trace_binding_valid": trace_binding_valid,
        "trace_events_consistent": trace_events_consistent,
        "task": {
            "task_id": task_id, "workflow": task.get("workflow"),
            "status": task.get("status"), "steps": task.get("steps"),
            "budget": task.get("budget"), "request_sha256": task.get("request_sha256"),
            "research_id": research_id,
            "research_ids": research_ids,
            "recovery_target": task.get("recovery_target"),
            "trace_ref": trace_ref,
            "checkpoint_names": sorted(task.get("checkpoints", {})),
        },
        "journal_verification": verification,
        "events": events,
        "model_calls": calls,
        "model_budget": model_budget,
        "model_budgets": model_budgets,
        "model_sessions": sessions,
        "evidence_manifest": evidence_manifest,
        "domain_evidence": domain,
    }
