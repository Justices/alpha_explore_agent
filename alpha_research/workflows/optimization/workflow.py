"""Read-only Optimization source baseline and independent branch records."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from alpha_research.state.store import atomic_write_json
from alpha_research.knowledge import KnowledgeService
from alpha_research.integrations.model import DecisionRecorder
from alpha_research.simulation.matching import SETTING_KEYS
from alpha_research.expressions.validator import Parser, ParseError, NON_FIELD_IDENTS, walk, validate, regular_operator_names
from alpha_research.expressions.super_alpha import validate_super_expressions
from alpha_research.qualification.pending_audit import record_pending_audit
from alpha_research.state.records import ResearchRecordStore
from alpha_research.qualification.service import CandidateQualificationService
from alpha_research.simulation.intent import create_simulation_intent, transition_simulation_intent


class OptimizationError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _code(value):
    return value.get("code") if isinstance(value, dict) else value


def _source_properties(details):
    """Only immutable Alpha definition fields, excluding changing performance metrics."""
    family = str(details.get("type") or "").upper()
    settings = details.get("settings") if isinstance(details.get("settings"), dict) else {}
    if family == "REGULAR":
        definition = {"regular": _code(details.get("regular"))}
    else:
        block = details.get("super") if isinstance(details.get("super"), dict) else {}
        definition = {
            "selection": _code(details.get("selection") or block.get("selection")),
            "combo": _code(details.get("combo") or block.get("combo")),
        }
    return {"id": details.get("id"), "type": family,
            "settings": settings, **definition}


def _source_properties_digest(details):
    return _digest(_source_properties(details))


def _put_advanced(store, record):
    """Persist a branch transition with a strictly increasing timestamp."""
    previous = store.get("candidate_branch", record["branch_id"])
    prior = previous["updated_at"]
    now = datetime.now(timezone.utc)
    last = datetime.fromisoformat(prior)
    if now <= last:
        from datetime import timedelta
        now = last + timedelta(microseconds=1)
    record["updated_at"] = now.isoformat()
    return store.put("candidate_branch", record, expected_updated_at=prior)


def prepare_source(gateway, state_root: Path, *, alpha_id: str,
                   objective: str | None = None, region: str | None = None):
    """Capture current platform details without changing the source Alpha."""
    if not isinstance(alpha_id, str) or not alpha_id.strip():
        raise OptimizationError("Alpha ID is required")
    details = gateway.call("get_alpha_details", alpha_id=alpha_id)
    if not isinstance(details, dict) or details.get("id") != alpha_id:
        raise OptimizationError("source Alpha details are unavailable")
    family = str(details.get("type") or "").upper()
    if family not in ("REGULAR", "SUPER"):
        raise OptimizationError("source Alpha family is unresolved")
    settings = details.get("settings")
    if not isinstance(settings, dict):
        raise OptimizationError("source Alpha settings are unavailable")
    source_region = settings.get("region")
    if not isinstance(source_region, str) or not source_region:
        raise OptimizationError("source Alpha Region is unresolved")
    if region is not None and region != source_region:
        raise OptimizationError("requested Region conflicts with source Alpha")
    if family == "REGULAR":
        expression = details.get("regular")
        if not isinstance(_code(expression), str) or not _code(expression).strip():
            raise OptimizationError("Regular source expression is unavailable")
    else:
        super_details = details.get("super") if isinstance(details.get("super"), dict) else {}
        selection = _code(details.get("selection") or super_details.get("selection"))
        combo = _code(details.get("combo") or super_details.get("combo"))
        if not isinstance(selection, str) or not selection.strip() or not isinstance(combo, str) or not combo.strip():
            raise OptimizationError("Super source expressions are unavailable")
    source_id = uuid4().hex
    observed_at = _now()
    root = Path(state_root).resolve() / "state" / "research_v1_1" / "optimization"
    snapshot_path = root / "snapshots" / f"{source_id}.json"
    atomic_write_json(snapshot_path, details)
    captured = {
        "schema_version": 1, "source_id": source_id,
        "alpha_id": alpha_id, "source_type": family.lower(),
        "region": source_region, "objective": objective,
        "snapshot_ref": str(snapshot_path), "details_sha256": _digest(details),
        "source_properties_sha256": _source_properties_digest(details),
        "observed_at": observed_at, "status": "captured",
        "created_at": observed_at, "updated_at": observed_at}
    path = ResearchRecordStore(state_root).put("optimization_source", captured)
    return {"source_id": captured["source_id"], "source_type": captured["source_type"],
            "alpha_id": alpha_id, "region": source_region,
            "source_ref": str(path), "snapshot_ref": str(snapshot_path),
            "details_sha256": captured["details_sha256"],
            "source_properties_sha256": captured["source_properties_sha256"]}


def create_branch(state_root: Path, *, source_ref: str, objective: str,
                  rationale: str, changes: list, effective_settings: dict):
    """Create an isolated draft. No Alpha or simulation endpoint is called."""
    source_path = Path(source_ref).resolve()
    store = ResearchRecordStore(state_root)
    if not source_path.is_relative_to((store.root / "optimization_source").resolve()):
        raise OptimizationError("source record must belong to this state root")
    try:
        source = store.get("optimization_source", source_path.stem)
        snapshot_path = Path(source["snapshot_ref"]).resolve()
        snapshot_root = (Path(state_root).resolve() / "state" /
                         "research_v1_1" / "optimization" / "snapshots").resolve()
        if not snapshot_path.is_relative_to(snapshot_root):
            raise OptimizationError("source snapshot is outside this state root")
        details = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OptimizationError("source snapshot is unreadable") from exc
    if (source.get("status") != "captured" or
            source.get("details_sha256") != _digest(details)):
        raise OptimizationError("source snapshot changed")
    if not isinstance(objective, str) or not objective.strip():
        raise OptimizationError("optimization objective is required")
    if not isinstance(rationale, str) or not rationale.strip():
        raise OptimizationError("candidate rationale is required")
    if not isinstance(changes, list) or not changes:
        raise OptimizationError("candidate needs a described change")
    if not isinstance(effective_settings, dict):
        raise OptimizationError("effective settings are required")
    for key in ("region", "delay", "universe"):
        source_value = details["settings"].get(key)
        if effective_settings.get(key) != source_value:
            raise OptimizationError(f"candidate {key} differs from the source Alpha")
    branch = {
        "schema_version": 1, "branch_id": uuid4().hex,
        "workflow": "optimization",
        "source_id": source["source_id"],
        "source_alpha_id": source["alpha_id"],
        "source_snapshot_ref": str(snapshot_path),
        "source_snapshot_sha256": source["details_sha256"],
        "source_properties_sha256": source["source_properties_sha256"],
        "source_type": source["source_type"],
        "objective": objective, "rationale": rationale,
        "changes": changes, "effective_settings": effective_settings,
        "prereg_ref": None, "simulation_intent_ref": None,
        "receipt_ref": None, "audit_ref": None,
        "status": "draft", "created_at": _now(), "updated_at": _now()}
    path = store.put("candidate_branch", branch)
    return {"branch_id": branch["branch_id"], "branch_ref": str(path),
            "source_alpha_id": source["alpha_id"], "status": "draft"}


def _fields(expression):
    try:
        return sorted({node.value for node, _ in walk(Parser(expression).parse())
                       if node.kind == "ident" and node.value not in NON_FIELD_IDENTS})
    except ParseError as exc:
        raise OptimizationError("source or candidate expression cannot be parsed") from exc


def _effective_settings(details):
    inverse = {value: key for key, value in SETTING_KEYS.items()}
    return {inverse.get(key, key): value for key, value in details["settings"].items()}


def _source_dataset(gateway, settings, source_fields):
    scope = {key: settings[key] for key in
             ("instrument_type", "region", "delay", "universe")}
    datasets = gateway.call("get_datasets", **scope)
    if not isinstance(datasets, list):
        raise OptimizationError("exact-scope Dataset catalog is unavailable")
    matches = []
    for row in datasets:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            continue
        dataset_id = row["id"]
        fields = gateway.call("get_datafields", dataset_id=dataset_id, **scope)
        if not isinstance(fields, list):
            raise OptimizationError("exact-scope field catalog is unavailable")
        ids = {field.get("id") for field in fields if isinstance(field, dict)}
        if set(source_fields) <= ids:
            matches.append((dataset_id, fields))
    if len(matches) != 1:
        raise OptimizationError("source Dataset cannot be uniquely verified")
    return matches[0]


def run_optimization(gateway, model, config: dict, state_root: Path, *,
                     alpha_id: str, objective: str, region: str | None = None,
                     execution_context=None):
    """Simulate one independent candidate, retaining immutable source evidence."""
    if not isinstance(objective, str) or not objective.strip():
        raise OptimizationError("optimization objective is required")
    run_id = uuid4().hex
    gateway.call("set_case", case_key=run_id, run_id=run_id)
    source_info = prepare_source(gateway, state_root, alpha_id=alpha_id,
                                 objective=objective, region=region)
    details = json.loads(Path(source_info["snapshot_ref"]).read_text(encoding="utf-8"))
    settings = _effective_settings(details)
    family = source_info["source_type"]
    operators = gateway.call("get_operators")
    if not isinstance(operators, list) or not operators:
        raise OptimizationError("live operator catalog is unavailable")
    dataset_id, fields = None, None
    if family == "regular":
        source_expression = _code(details["regular"])
        source_fields = _fields(source_expression)
        if not source_fields:
            raise OptimizationError("source Regular fields cannot be resolved")
        dataset_id, fields = _source_dataset(gateway, settings, source_fields)
    else:
        source_expression = None
        source_fields = []
    root = Path(state_root).resolve()
    knowledge = KnowledgeService(root / "state", root / "runs", config)
    context = {"objective": objective, "source_alpha": details,
               "source_type": family, "source_fields": source_fields,
               "verified_dataset_id": dataset_id,
               "live_operator_catalog": operators}
    pack = knowledge.build_pack("optimize_alpha", context)
    pack_path = root / "state" / "research_v1_1" / "optimization" / f"{run_id}_knowledge.json"
    atomic_write_json(pack_path, pack)
    recorder = DecisionRecorder(model, root / "state")
    recorder.begin_session(research_id=f"optimization:{run_id}")
    proposal = recorder.complete_json("optimize_alpha", {**context, "knowledge_pack": pack},
                                      knowledge_pack_ref=str(pack_path))
    for key in ("rationale", "hypothesis", "failure_criterion"):
        if not isinstance(proposal.get(key), str) or not proposal[key].strip():
            raise OptimizationError(f"optimization proposal lacks {key}")
    if not isinstance(proposal.get("changes"), list) or not proposal["changes"]:
        raise OptimizationError("optimization changes are missing")
    if family == "regular":
        expression = proposal.get("expression")
        if not isinstance(expression, str) or not expression.strip():
            raise OptimizationError("Regular candidate expression is missing")
        if not set(_fields(expression)) <= set(source_fields):
            raise OptimizationError("candidate introduced fields outside the source")
        catalog = {}
        for row in fields:
            if isinstance(row, dict) and row.get("id") in source_fields:
                owner = row.get("dataset")
                owner = owner.get("id") if isinstance(owner, dict) else owner
                catalog[row["id"]] = {"dataset": owner, "type": row.get("type")}
        verdict = validate(expression, dataset_id, catalog,
                           regular_operator_names(operators),
                           operator_definitions=operators)
        if verdict["verdict"] != "PASS" or not verdict["fields"]:
            raise OptimizationError("Regular candidate failed local validation")
        research = proposal.get("research")
        if not isinstance(research, dict):
            raise OptimizationError("Regular candidate research evidence is missing")
    else:
        selection, combo = proposal.get("selection"), proposal.get("combo")
        validate_super_expressions(selection, combo, operators)
        research = {key: proposal[key] for key in
                    ("hypothesis", "rationale", "failure_criterion")}
    branch_info = create_branch(
        state_root, source_ref=source_info["source_ref"], objective=objective,
        rationale=proposal["rationale"], changes=proposal["changes"],
        effective_settings=settings)
    store = ResearchRecordStore(state_root)
    branch = store.get("candidate_branch", branch_info["branch_id"])
    prereg_path = root / "state" / "research_v1_1" / "optimization" / f"{run_id}_prereg.json"
    atomic_write_json(prereg_path, {"source": source_info, "proposal": proposal,
                                    "dataset_id": dataset_id, "settings": settings,
                                    "registered_at": _now()})
    batch_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    if family == "regular":
        request = {"dataset_id": dataset_id, "label": "production",
                   "variants": [{"name": "optimization_candidate",
                                 "expression": expression, "research": research}]}
    else:
        request = {"selection": selection, "combo": combo, "research": research}
    intent_path = prereg_path.with_name(f"{run_id}_simulation_intent.json")
    create_simulation_intent(
        intent_path, workflow="optimization", run_id=run_id,
        batch_id=batch_id, request=request, settings=settings)
    if execution_context is not None:
        bind_target = execution_context.get("bind_recovery_target")
        if callable(bind_target):
            bind_target("simulation_intent", intent_path)
    branch.update(status="simulation_prepared", prereg_ref=str(prereg_path),
                  simulation_intent_ref=str(intent_path))
    _put_advanced(store, branch)
    transition_simulation_intent(
        intent_path, expected_status="PREPARED", status="POSTING")
    branch.update(status="simulation_posting")
    _put_advanced(store, branch)
    try:
        if family == "regular":
            result = gateway.call("simulate_batch", batch_id=batch_id,
                                  label="production", dataset_id=dataset_id,
                                  settings=settings, variants=request["variants"])
        else:
            result = gateway.call("simulate_super", batch_id=batch_id,
                                  settings=settings, selection=selection,
                                  combo=combo, research=research)
    except Exception:
        transition_simulation_intent(
            intent_path, expected_status="POSTING", status="OUTCOME_UNKNOWN")
        branch.update(status="outcome_unknown")
        _put_advanced(store, branch)
        raise
    response_bytes = json.dumps(result, ensure_ascii=False, sort_keys=True,
                                default=str).encode("utf-8")
    response_path = prereg_path.with_name(f"{run_id}_response.json")
    atomic_write_json(response_path, result)
    transition_simulation_intent(
        intent_path, expected_status="POSTING", status="RESPONSE_RECEIVED",
        response_sha256=hashlib.sha256(response_bytes).hexdigest(),
        response_ref=str(response_path))
    rows = result.get("results") if isinstance(result, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict) or not rows[0].get("alpha_id"):
        transition_simulation_intent(
            intent_path, expected_status="RESPONSE_RECEIVED", status="OUTCOME_UNKNOWN")
        previous = branch["updated_at"]
        branch.update(status="outcome_unknown", updated_at=_now())
        _put_advanced(store, branch)
        raise OptimizationError("candidate simulation readback is incomplete")
    readback_path = prereg_path.with_name(f"{run_id}_readback.json")
    atomic_write_json(readback_path, rows[0])
    transition_simulation_intent(
        intent_path, expected_status="RESPONSE_RECEIVED", status="READ_BACK",
        receipt_ref=str(readback_path))
    # Re-read the source definition after simulation. Performance metrics may
    # change independently, so compare only identity, family, settings and code.
    latest_source = gateway.call("get_alpha_details", alpha_id=alpha_id)
    if (not isinstance(latest_source, dict) or
            _source_properties_digest(latest_source) !=
            source_info["source_properties_sha256"]):
        branch.update(status="source_integrity_unresolved",
                      receipt_ref=str(readback_path),
                      simulation_intent_ref=str(intent_path),
                      candidate_alpha_id=rows[0]["alpha_id"],
                      unresolved=["source_alpha_definition_changed_or_unreadable"])
        _put_advanced(store, branch)
        raise OptimizationError(
            "source Alpha definition could not be verified unchanged after simulation")
    audit_path = prereg_path.with_name(f"{run_id}_audit.json")
    audit = record_pending_audit(
        rows[0]["details"], workflow="optimization", source=details)
    audit["source_properties_sha256"] = source_info["source_properties_sha256"]
    audit["source_snapshot_sha256"] = source_info["details_sha256"]
    atomic_write_json(audit_path, audit)
    report_path = prereg_path.with_name(f"{run_id}_optimization_report.json")
    report = {
        "schema_version": 1, "workflow": "optimization",
        "source_alpha_id": alpha_id, "source_type": family,
        "source_ref": source_info["source_ref"],
        "source_snapshot_ref": source_info["snapshot_ref"],
        "source_snapshot_sha256": source_info["details_sha256"],
        "source_properties_sha256": source_info["source_properties_sha256"],
        "candidate_branch_id": branch["branch_id"],
        "candidate_alpha_id": rows[0]["alpha_id"],
        "objective": objective, "hypothesis": proposal["hypothesis"],
        "rationale": proposal["rationale"], "changes": proposal["changes"],
        "effective_settings": settings, "dataset_id": dataset_id,
        "simulation_intent_ref": str(intent_path),
        "readback_ref": str(readback_path), "audit_ref": str(audit_path),
        "source_unchanged_verified": True,
        "candidate_status": "simulated_pending_audit",
        "pending": audit["pending"], "created_at": _now()}
    atomic_write_json(report_path, report)
    qualification = CandidateQualificationService(
        gateway, state_root, config).qualify_live(
            workflow="optimization", alpha_id=rows[0]["alpha_id"],
            candidate_ref=branch["branch_id"], source_ref=str(report_path),
            evidence_refs={"source_snapshot": source_info["snapshot_ref"],
                           "simulation_intent": str(intent_path),
                           "readback": str(readback_path), "audit": str(audit_path),
                           "optimization_report": str(report_path)},
            fallback_details=rows[0].get("details"))
    report["candidate_id"] = qualification["candidate_id"]
    report["candidate_status"] = qualification["candidate_status"]
    report["preferred_status"] = qualification["preferred"]["status"]
    report["candidate_record_ref"] = qualification["candidate_ref"]
    report["preferred_record_ref"] = qualification["preferred_ref"]
    atomic_write_json(report_path, report)
    branch.update(status="candidate_evaluated", receipt_ref=str(readback_path),
                  simulation_intent_ref=str(intent_path), response_ref=str(response_path),
                  audit_ref=str(audit_path), runner=result.get("runner"),
                  report_ref=str(report_path), candidate_alpha_id=rows[0]["alpha_id"],
                  candidate_id=qualification["candidate_id"],
                  candidate_status=qualification["candidate_status"],
                  preferred_status=qualification["preferred"]["status"],
                  candidate_record_ref=qualification["candidate_ref"],
                  preferred_record_ref=qualification["preferred_ref"],
                  budget={"limits": {"experiments": 1},
                          "reserved": {"experiments": 0},
                          "used": {"experiments": 1}},
                  source_unchanged_verified=True)
    _put_advanced(store, branch)
    return {"branch_id": branch["branch_id"], "branch_ref": branch_info["branch_ref"],
            "source_alpha_id": alpha_id, "candidate_alpha_id": rows[0]["alpha_id"],
            "candidate_id": qualification["candidate_id"],
            "candidate_status": qualification["candidate_status"],
            "preferred_status": qualification["preferred"]["status"],
            "candidate_ref": qualification["candidate_ref"],
            "preferred_ref": qualification["preferred_ref"],
            "report_ref": str(report_path), "source_unchanged_verified": True,
            "status": branch["status"]}
