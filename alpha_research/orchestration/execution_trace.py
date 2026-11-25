"""Read-only execution trace projection across Task, domain and model evidence."""
import hashlib
import json
from pathlib import Path

from alpha_research.orchestration.event_journal import ExecutionEventJournal
from alpha_research.orchestration.engine import MainOrchestrator
from alpha_research.state.records import ResearchRecordStore


def _read_object(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _safe_model_call(path, record):
    allowed = (
        "call_id", "ts", "task", "agent_role", "agent_backend", "model",
        "session_id", "research_id", "batch_ref", "status",
        "context_sha256", "route_sha256", "response_sha256",
        "context_ref", "response_ref", "record_ref",
    )
    row = {key: record[key] for key in allowed if key in record}
    row["record_path"] = str(path)
    return row


def inspect_execution_trace(state_root, task_id):
    """Verify the Task event chain and correlate existing model/domain records."""
    root = Path(state_root).resolve()
    task = MainOrchestrator(root).load(task_id)
    journal = ExecutionEventJournal(root, task_id)
    events = journal.read_events()
    verification = journal.verify()
    research_id = (task.get("research_identity") or {}).get("research_id")

    calls = []
    calls_root = root / "logs" / "model_calls"
    if calls_root.exists() and research_id:
        for path in sorted(calls_root.glob("*.record.json")):
            record = _read_object(path)
            if record and record.get("research_id") == research_id:
                calls.append(_safe_model_call(path, record))

    session_ids = sorted({row.get("session_id") for row in calls
                          if isinstance(row.get("session_id"), str)})
    sessions = []
    for session_id in session_ids:
        path = root / "logs" / "model_sessions" / f"{session_id}.json"
        index = _read_object(path)
        sessions.append({
            "session_id": session_id,
            "status": "READABLE" if index else "MISSING_OR_UNREADABLE",
            "index_ref": str(path),
            "call_ids": index.get("call_ids", []) if index else [],
            "batches": index.get("batches", []) if index else [],
        })

    domain = {"campaign": None, "super_run": None,
              "optimization": None, "simulation_intents": []}
    if isinstance(research_id, str) and research_id.startswith("regular:campaign:"):
        campaign_id = research_id.removeprefix("regular:campaign:")
        path = root / "state" / "research_v1_1" / "campaign_runs" / f"{campaign_id}.json"
        saved = _read_object(path)
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
                    key: {"status": value.get("status"),
                          "review_ref": str(
                              path.parent / "dataset_reviews" / campaign_id /
                              hashlib.sha256(json.dumps(
                                  {"dataset_id": key}, sort_keys=True,
                                  ensure_ascii=False, separators=(",", ":")
                              ).encode("utf-8")).hexdigest()[:32] / "review.json")}
                    for key, value in saved.get("dataset_reviews", {}).items()
                    if isinstance(value, dict)
                },
                "campaign_state_ref": str(path),
                "campaign_summary_ref": str(path.with_name(f"{campaign_id}_summary.json")),
            }
    elif isinstance(research_id, str) and research_id.startswith("super:"):
        run_id = research_id.removeprefix("super:")
        path = root / "state" / "research_v1_1" / "super_runs" / f"{run_id}.json"
        saved = _read_object(path)
        if saved:
            domain["super_run"] = {
                key: saved.get(key) for key in (
                    "run_id", "status", "region", "objective", "batch_id",
                    "alpha_id", "candidate_id", "candidate_status",
                    "preferred_status", "prereg_ref", "simulation_intent_ref",
                    "response_ref", "readback_ref", "audit_ref")
                if key in saved
            }
            domain["super_run"]["record_ref"] = str(path)
    elif isinstance(research_id, str) and research_id.startswith("optimization:"):
        run_id = research_id.removeprefix("optimization:")
        base = root / "state" / "research_v1_1" / "optimization"
        report_path = base / f"{run_id}_optimization_report.json"
        report = _read_object(report_path)
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
                "source_snapshot_ref", "simulation_intent_ref", "readback_ref",
                "audit_ref", "source_unchanged_verified")} if report else None),
            "branch": ({key: branch.get(key) for key in (
                "branch_id", "status", "source_id", "source_alpha_id",
                "source_snapshot_ref", "candidate_alpha_id", "candidate_id",
                "candidate_status", "preferred_status", "simulation_intent_ref",
                "receipt_ref", "audit_ref", "report_ref")} if branch else None),
            "source": ({key: source.get(key) for key in (
                "source_id", "alpha_id", "source_type", "snapshot_ref",
                "details_sha256", "source_properties_sha256")} if source else None),
        }

    intent_refs = set()
    for event in events:
        payload = event.get("payload") or {}
        for key, value in payload.items():
            if key.endswith("_ref") and isinstance(value, str):
                intent_refs.add(value)
    recovery_target = task.get("recovery_target") or {}
    if recovery_target.get("kind") == "simulation_intent":
        intent_refs.add(recovery_target.get("ref"))
    for ref in sorted(value for value in intent_refs if value):
        path = Path(ref)
        if not path.is_absolute():
            path = root / path
        try:
            path = path.resolve(strict=True)
            if not path.is_relative_to(root):
                continue
        except (OSError, RuntimeError):
            continue
        intent = _read_object(path)
        if intent and intent.get("workflow") in ("regular", "super", "optimization"):
            body = {
                "workflow": intent.get("workflow"),
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

    return {
        "schema_version": 1,
        "status": ("TRACE_VERIFIED" if verification["status"] == "VERIFIED"
                   and verification["event_count"] > 0 else "TRACE_INCOMPLETE"),
        "task": {
            "task_id": task_id, "workflow": task.get("workflow"),
            "status": task.get("status"), "steps": task.get("steps"),
            "budget": task.get("budget"), "request_sha256": task.get("request_sha256"),
            "research_id": research_id,
            "recovery_target": task.get("recovery_target"),
            "trace_ref": (task.get("trace") or {}).get("journal_ref"),
            "checkpoint_names": sorted(task.get("checkpoints", {})),
        },
        "journal_verification": verification,
        "events": events,
        "model_calls": calls,
        "model_sessions": sessions,
        "domain_evidence": domain,
    }
