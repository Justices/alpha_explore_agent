"""BRAIN-native Super research preflight; no Regular Dataset assumptions."""

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from ...knowledge import KnowledgeService
from ...model_client import DecisionRecorder
from ...state_store import atomic_write_json
from ...validator import Parser, ParseError
from ..audit import record_pending_audit


class SuperScopeError(ValueError):
    pass


def validate_super_expressions(selection: str, combo: str, operator_rows: list):
    """Check syntax and live operator scope without imposing Regular rules."""
    if not isinstance(operator_rows, list) or not operator_rows:
        raise SuperScopeError("live Super operator catalog is unavailable")
    supported = {
        scope: {row["name"] for row in operator_rows
                if isinstance(row, dict) and isinstance(row.get("name"), str)
                and isinstance(row.get("scope"), list)
                and scope in row["scope"]}
        for scope in ("SELECTION", "COMBO")}
    for label, expression, scope in (
            ("selection", selection, "SELECTION"),
            ("combo", combo, "COMBO")):
        if not isinstance(expression, str) or not expression.strip():
            raise SuperScopeError(f"{label} expression is required")
        try:
            ast = Parser(expression).parse()
        except ParseError as exc:
            raise SuperScopeError(f"{label} syntax is invalid: {exc}") from exc
        stack = [ast]
        while stack:
            node = stack.pop()
            if node.kind == "call" and node.value not in supported[scope]:
                raise SuperScopeError(
                    f"{label} operator is not live-supported: {node.value}")
            stack.extend(node.args)
            stack.extend(node.kwargs.values())
            stack.extend(node.children)
    return {"selection": "valid", "combo": "valid"}


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
    return {"run_id": run_id, "status": record["status"],
            "eligible_alpha_count": len(eligible),
            "unresolved_count": len(unresolved), "record_ref": str(path)}


def run_super_research(gateway, model, config: dict, state_root: Path, *,
                       region: str, objective: str, delay: int | None = None):
    """One evidence-registered native Super experiment; no auto-submission."""
    run_id = uuid4().hex
    gateway.call("set_case", case_key=run_id, run_id=run_id)
    prepared = prepare_super_run(
        gateway, state_root, region=region, objective=objective,
        delay=delay, run_id=run_id)
    run_path = Path(prepared["record_ref"])
    run = json.loads(run_path.read_text(encoding="utf-8"))
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
    recorder.begin_session()
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
    run["status"] = "simulation_pending"
    run["prereg_ref"] = str(prereg_path)
    run["batch_id"] = batch_id
    atomic_write_json(run_path, run)
    result = gateway.call(
        "simulate_super", batch_id=batch_id, settings=settings,
        selection=proposal["selection"], combo=proposal["combo"],
        research={key: proposal[key] for key in
                  ("hypothesis", "rationale", "failure_criterion")})
    rows = result.get("results") if isinstance(result, dict) else None
    if not isinstance(rows, list) or len(rows) != 1 or not rows[0].get("alpha_id"):
        raise SuperScopeError("Super simulation readback is incomplete")
    run["status"] = "simulated_pending_audit"
    run["alpha_id"] = rows[0]["alpha_id"]
    run["runner"] = result.get("runner")
    run["readback_ref"] = str(run_path.with_name(f"{run_id}_{batch_id}_readback.json"))
    atomic_write_json(Path(run["readback_ref"]), rows[0])
    audit_path = run_path.with_name(f"{run_id}_{batch_id}_audit.json")
    atomic_write_json(audit_path, record_pending_audit(
        rows[0]["details"], workflow="super"))
    run["audit_ref"] = str(audit_path)
    atomic_write_json(run_path, run)
    return {"run_id": run_id, "alpha_id": run["alpha_id"],
            "status": run["status"], "record_ref": str(run_path)}
