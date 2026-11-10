"""BRAIN-native Super research preflight; no Regular Dataset assumptions."""

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from alpha_research.knowledge import KnowledgeService
from alpha_research.integrations.model import DecisionRecorder
from alpha_research.state.store import atomic_write_json
from alpha_research.expressions.super_alpha import SuperScopeError, validate_super_expressions
from alpha_research.qualification.pending_audit import record_pending_audit
from alpha_research.simulation.intent import create_simulation_intent, transition_simulation_intent
from alpha_research.state.records import ResearchRecordStore
from alpha_research.qualification.service import CandidateQualificationService


def prepare_super_run(gateway, state_root: Path, *, region: str,
                      objective: str, delay: int | None = None,
                      run_id: str | None = None):
    if not isinstance(region, str) or not region:
        raise SuperScopeError("Region is required")
    if not isinstance(objective, str) or not objective.strip():
        raise SuperScopeError("objective is required")
    options = gateway.call("get_platform_setting_options")
    rows = options.get("instrument_options") if isinstance(options, dict) else None
    if not isinstance(rows, list):
        raise SuperScopeError("platform settings are unreadable")
    allowed_delays = sorted({row["Delay"] for row in rows if isinstance(row, dict)
                             and row.get("InstrumentType") == "EQUITY"
                             and row.get("Region") == region
                             and isinstance(row.get("Delay"), int)})
    if not allowed_delays or delay is not None and delay not in allowed_delays:
        raise SuperScopeError("Region/Delay is not supported by platform settings")
    schema = gateway.call("get_simulation_settings_schema")
    if not isinstance(schema, dict):
        raise SuperScopeError("Super settings schema is unavailable")
    super_keys = ("selectionHandling", "selectionLimit", "componentActivation")
    super_schema = {key: schema[key] for key in super_keys if key in schema}
    if not super_schema:
        raise SuperScopeError("dedicated Super settings are absent from platform schema")
    alphas = []
    offset = 0
    page_size = 100
    while True:
        page = gateway.call("get_user_alphas", stage="OS",
                            limit=page_size, offset=offset)
        if not isinstance(page, dict) or not isinstance(page.get("results"), list):
            raise SuperScopeError("submitted Alpha catalog is unreadable")
        batch = page["results"]
        alphas.extend(batch)
        offset += len(batch)
        count = page.get("count")
        if isinstance(count, int) and offset >= count:
            break
        if not batch:
            if count is not None and offset < count:
                raise SuperScopeError("submitted Alpha catalog is incomplete")
            break
        if len(batch) < page_size and count is None:
            break
        if offset >= 100000:
            raise SuperScopeError("submitted Alpha catalog exceeds safety limit")
    eligible = []
    unresolved = []
    for row in alphas:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            unresolved.append({"reason": "unreadable_alpha_row"})
            continue
        if str(row.get("status") or "").upper() != "ACTIVE":
            continue
        settings = row.get("settings")
        if not isinstance(settings, dict) or not settings.get("region"):
            unresolved.append({"alpha_id": row["id"],
                               "reason": "active_alpha_settings_unreadable"})
            continue
        if settings["region"] != region:
            continue
        if delay is not None and settings.get("delay") != delay:
            continue
        eligible.append({"alpha_id": row["id"], "type": row.get("type"),
                         "region": settings["region"], "delay": settings.get("delay")})
    run_id = run_id or uuid4().hex
    record = {"schema_version": 1, "run_id": run_id,
              "workflow": "super", "region": region, "objective": objective,
              "requested_delay": delay, "allowed_delays": allowed_delays,
              "platform_super_schema": super_schema,
              "active_submitted_pool": eligible, "unresolved": unresolved,
              "observed_at": datetime.now(timezone.utc).isoformat(),
              "status": "prepared" if eligible and not unresolved else "unresolved"}
    path = (Path(state_root).resolve() / "state" / "research_v1_1" /
            "super_runs" / f"{run_id}.json")
    atomic_write_json(path, record)
    now = record["observed_at"]
    ResearchRecordStore(state_root).put("super_run", {
        "schema_version": 1, "run_id": run_id, "workflow": "super",
        "region": region, "objective": objective,
        "budget": {"limits": {"experiments": 1},
                   "reserved": {"experiments": 0},
                   "used": {"experiments": 0}},
        "platform_record_ref": str(path),
        "platform_super_schema": super_schema,
        "active_submitted_pool_ref": str(path),
        "simulation_intent_ref": None, "readback_ref": None,
        "audit_ref": None, "unresolved": unresolved,
        "status": record["status"], "created_at": now, "updated_at": now})
    return {"run_id": run_id, "status": record["status"],
            "eligible_alpha_count": len(eligible),
            "unresolved_count": len(unresolved), "record_ref": str(path)}


def run_super_research(gateway, model, config: dict, state_root: Path, *,
                       region: str, objective: str, delay: int | None = None,
                       execution_context=None):
    """One evidence-registered native Super experiment; no auto-submission."""
    run_id = uuid4().hex
    gateway.call("set_case", case_key=run_id, run_id=run_id)
    prepared = prepare_super_run(
        gateway, state_root, region=region, objective=objective,
        delay=delay, run_id=run_id)
    run_path = Path(prepared["record_ref"])
    run = json.loads(run_path.read_text(encoding="utf-8"))
    store = ResearchRecordStore(state_root)
    shared = store.get("super_run", run_id)
    def persist_shared(**changes):
        nonlocal shared
        previous = shared["updated_at"]
        now = datetime.now(timezone.utc)
        last = datetime.fromisoformat(previous)
        if now <= last:
            from datetime import timedelta
            now = last + timedelta(microseconds=1)
        shared.update(changes)
        shared["updated_at"] = now.isoformat()
        store.put("super_run", shared, expected_updated_at=previous)
    if prepared["status"] != "prepared":
        return prepared
    operators = gateway.call("get_operators")
    if not isinstance(operators, list):
        raise SuperScopeError("live operator catalog is unavailable")
    root = Path(state_root).resolve()
    knowledge = KnowledgeService(root / "state", root / "runs", config)
    context = {
        "objective": objective, "scope": {"region": region, "delay": delay},
        "active_submitted_pool": run["active_submitted_pool"],
        "platform_super_schema": run["platform_super_schema"],
        "allowed_delays": run["allowed_delays"],
        "operator_catalog": [row for row in operators if isinstance(row, dict)
                             and isinstance(row.get("scope"), list)
                             and any(scope in row["scope"] for scope in
                                     ("SELECTION", "COMBO"))],
    }
    rule_pack = knowledge.build_pack("construct_super", context)
    pack_path = run_path.with_name(f"{run_id}_knowledge_pack.json")
    atomic_write_json(pack_path, rule_pack)
    recorder = DecisionRecorder(model, root / "state")
    recorder.begin_session(research_id=f"super:{run_id}")
    proposal = recorder.complete_json(
        "construct_super", {**context, "knowledge_pack": rule_pack},
        knowledge_pack_ref=str(pack_path))
    for key in ("selection", "combo", "hypothesis", "rationale",
                "failure_criterion"):
        if not isinstance(proposal.get(key), str) or not proposal[key].strip():
            raise SuperScopeError(f"Super proposal lacks {key}")
    settings = proposal.get("settings")
    if not isinstance(settings, dict) or settings.get("region") != region:
        raise SuperScopeError("Super proposal changed the required Region")
    if delay is not None and settings.get("delay") != delay:
        raise SuperScopeError("Super proposal changed the required Delay")
    for key in ("universe", "neutralization", "decay", "truncation",
                "pasteurization"):
        fixed = (config.get("scope") or {}).get(key)
        if fixed is not None and settings.get(key) != fixed:
            raise SuperScopeError(f"Super proposal changed configured {key}")
    validation = validate_super_expressions(
        proposal["selection"], proposal["combo"], operators)
    batch_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    prereg = {
        "workflow": "super", "run_id": run_id, "batch_id": batch_id,
        "region": region, "objective": objective, "proposal": proposal,
        "validation": validation, "rule_pack_ref": str(pack_path),
        "platform_pool_observed_at": run["observed_at"],
        "registered_at": datetime.now(timezone.utc).isoformat()}
    prereg_path = run_path.with_name(f"{run_id}_{batch_id}_prereg.json")
    atomic_write_json(prereg_path, prereg)
    run["prereg_ref"] = str(prereg_path)
    run["batch_id"] = batch_id
    intent_path = run_path.with_name(f"{run_id}_{batch_id}_simulation_intent.json")
    request = {
        "selection": proposal["selection"], "combo": proposal["combo"],
        "research": {key: proposal[key] for key in
                     ("hypothesis", "rationale", "failure_criterion")},
    }
    create_simulation_intent(
        intent_path, workflow="super", run_id=run_id, batch_id=batch_id,
        request=request, settings=settings)
    if execution_context is not None:
        bind_target = execution_context.get("bind_recovery_target")
        if callable(bind_target):
            bind_target("simulation_intent", intent_path)
    run["simulation_intent_ref"] = str(intent_path)
    run["status"] = "simulation_prepared"
    atomic_write_json(run_path, run)
    persist_shared(status="simulation_prepared",
                   simulation_intent_ref=str(intent_path),
                   budget={"limits": {"experiments": 1},
                           "reserved": {"experiments": 1},
                           "used": {"experiments": 0}})
    transition_simulation_intent(
        intent_path, expected_status="PREPARED", status="POSTING")
    run["status"] = "simulation_posting"
    atomic_write_json(run_path, run)
    persist_shared(status="simulation_posting")
    try:
        result = gateway.call(
            "simulate_super", batch_id=batch_id, settings=settings,
            selection=proposal["selection"], combo=proposal["combo"],
            research=request["research"])
    except Exception:
        transition_simulation_intent(
            intent_path, expected_status="POSTING", status="OUTCOME_UNKNOWN")
        run["status"] = "outcome_unknown"
        atomic_write_json(run_path, run)
        persist_shared(status="outcome_unknown",
                       unresolved=["platform_simulation_outcome_requires_reconciliation"])
        raise
    response_bytes = json.dumps(result, ensure_ascii=False, sort_keys=True,
                                default=str).encode("utf-8")
    response_path = run_path.with_name(f"{run_id}_{batch_id}_response.json")
    atomic_write_json(response_path, result)
    transition_simulation_intent(
        intent_path, expected_status="POSTING", status="RESPONSE_RECEIVED",
        response_sha256=__import__("hashlib").sha256(response_bytes).hexdigest(),
        response_ref=str(response_path))
    run["response_ref"] = str(response_path)
    atomic_write_json(run_path, run)
    rows = result.get("results") if isinstance(result, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or not rows[0].get("alpha_id"):
        transition_simulation_intent(
            intent_path, expected_status="RESPONSE_RECEIVED", status="OUTCOME_UNKNOWN")
        run["status"] = "outcome_unknown"
        atomic_write_json(run_path, run)
        persist_shared(status="outcome_unknown",
                       unresolved=["simulation_response_missing_alpha_readback"])
        raise SuperScopeError("Super simulation readback is incomplete")
    run["alpha_id"] = rows[0]["alpha_id"]
    run["runner"] = result.get("runner")
    run["readback_ref"] = str(run_path.with_name(f"{run_id}_{batch_id}_readback.json"))
    atomic_write_json(Path(run["readback_ref"]), rows[0])
    transition_simulation_intent(
        intent_path, expected_status="RESPONSE_RECEIVED", status="READ_BACK",
        receipt_ref=run["readback_ref"])
    audit_path = run_path.with_name(f"{run_id}_{batch_id}_audit.json")
    atomic_write_json(audit_path, record_pending_audit(
        rows[0]["details"], workflow="super"))
    run["audit_ref"] = str(audit_path)
    run["status"] = "simulated_pending_audit"
    atomic_write_json(run_path, run)
    qualification = CandidateQualificationService(
        gateway, state_root, config).qualify_live(
            workflow="super", alpha_id=run["alpha_id"], candidate_ref=run_id,
            source_ref=str(audit_path),
            evidence_refs={"simulation_intent": str(intent_path),
                           "readback": run["readback_ref"], "audit": str(audit_path),
                           "prereg": str(prereg_path)},
            fallback_details=rows[0].get("details"))
    run["status"] = "candidate_evaluated"
    run["candidate_id"] = qualification["candidate_id"]
    run["candidate_status"] = qualification["candidate_status"]
    run["preferred_status"] = qualification["preferred"]["status"]
    atomic_write_json(run_path, run)
    persist_shared(status="candidate_evaluated",
                   candidate_alpha_id=run["alpha_id"],
                   candidate_id=qualification["candidate_id"],
                   candidate_status=qualification["candidate_status"],
                   preferred_status=qualification["preferred"]["status"],
                   candidate_record_ref=qualification["candidate_ref"],
                   preferred_record_ref=qualification["preferred_ref"],
                   readback_ref=run["readback_ref"], audit_ref=str(audit_path),
                   budget={"limits": {"experiments": 1},
                           "reserved": {"experiments": 0},
                           "used": {"experiments": 1}})
    return {"run_id": run_id, "alpha_id": run["alpha_id"],
            "candidate_id": qualification["candidate_id"],
            "candidate_status": qualification["candidate_status"],
            "preferred_status": qualification["preferred"]["status"],
            "candidate_ref": qualification["candidate_ref"],
            "preferred_ref": qualification["preferred_ref"],
            "status": run["status"], "record_ref": str(run_path)}
