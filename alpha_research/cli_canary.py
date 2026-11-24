"""One fixed Regular Alpha canary with pre-POST durable reservation.

This is an explicitly authorized legacy-domain integration test, NOT a P7
production-orchestrator grant. No model invocation, auto retry, tag PATCH or
Alpha submission. An ambiguous POST outcome is never automatically replayed.
"""
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from alpha_research.expressions.validator import regular_operator_names, validate
from alpha_research.orchestration.mock_workspace import marker_path
from alpha_research.state.store import StopSignals, atomic_write_json, file_sha256
from alpha_research.state.writer_lock import SingleWriterLock
from alpha_research.workflows.regular.settings_validation import _validate_test_period

CANARY_COMMANDS = frozenset({"canary-prepare", "canary-execute", "canary-status", "canary-review"})
DATASET = "analyst10"
FIELD = "analyst_revision_ratio_to_close_ebitda_q1"
EXPRESSION = f"rank({FIELD})"
SCOPE = {"instrument_type": "EQUITY", "region": "USA", "delay": 1, "universe": "TOP3000"}
RESEARCH = {
    "idea": "Positive EBITDA estimate revisions may precede price adjustment.",
    "data_rationale": "analyst10 SmartEstimate measures analyst earnings expectation updates.",
    "operator_rationale": "rank standardizes revision signals across the current equity cross-section.",
    "field_relationship": "One verified MATRIX field, no combined data sources or field arithmetic.",
    "settings_rationale": "Explicit scope and effective settings verified against live BRAIN choices.",
    "expected_behavior": "Stocks with stronger positive revisions may earn higher future relative returns.",
    "failure_criterion": "No reliable risk-adjusted return, excessive turnover or insufficient coverage.",
}


class CanaryError(RuntimeError):
    pass


def _plan_path(root, plan_id):
    if not isinstance(plan_id, str) or re.fullmatch("[a-f0-9]{32}", plan_id) is None:
        raise CanaryError("invalid canary plan ID")
    return root / "state" / "research_v1_1" / "plans" / (plan_id + ".json")


def _canary_path(root, plan_id):
    _plan_path(root, plan_id)
    return root / "state" / "canary" / (plan_id + ".json")


def _read(path):
    if path.is_symlink() or not path.is_file():
        raise CanaryError("required canary record is missing or symlinked")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CanaryError("canary record is not readable JSON") from exc
    if not isinstance(value, dict):
        raise CanaryError("canary record has an invalid shape")
    return value


def _verify_plan(root, plan_id, expected_digest=None):
    path = _plan_path(root, plan_id)
    plan = _read(path)
    if (plan.get("plan_id") != plan_id
            or plan.get("status") != "planned"
            or plan.get("coverage_complete") is not True
            or plan.get("unresolved") != []
            or plan.get("region") != "USA"
            or plan.get("requested") != {
                "dataset_ids": [DATASET], "category": None,
                "delay": 1, "universe": "TOP3000",
            }):
        raise CanaryError("saved Dataset plan is not fully admitted")
    datasets = plan.get("datasets")
    if (not isinstance(datasets, list) or len(datasets) != 1
            or not isinstance(datasets[0], dict)
            or datasets[0].get("dataset_id") != DATASET
            or datasets[0].get("category") != "analyst"):
        raise CanaryError("pinned Dataset identity was not confirmed")
    combinations = datasets[0].get("combinations")
    exact_scope = {"region": "USA", "delay": 1, "universe": "TOP3000"}
    if (not isinstance(combinations, list) or len(combinations) != 1
            or combinations[0].get("detail_scope") != exact_scope
            or combinations[0].get("scope_evidence_source") != "exact_catalog"):
        raise CanaryError("pinned Dataset exact-scope proof was not confirmed")
    digest = file_sha256(path)
    if expected_digest is not None and digest != expected_digest:
        raise CanaryError("original scope plan changed after preparation")
    return digest


def _effective_settings(options):
    rows = options.get("instrument_options") if isinstance(options, dict) else None
    schema = options.get("simulation_settings_schema") if isinstance(options, dict) else None
    if not isinstance(rows, list) or not isinstance(schema, dict):
        raise CanaryError("live settings metadata unavailable")
    matched = [r for r in rows if isinstance(r, dict)
               and r.get("InstrumentType") == "EQUITY"
               and r.get("Region") == "USA" and r.get("Delay") == 1
               and "TOP3000" in (r.get("Universe") or [])]
    if len(matched) != 1 or "INDUSTRY" not in (matched[0].get("Neutralization") or []):
        raise CanaryError("INDUSTRY neutralization is not live-verified for this scope")
    for key, value in (("decay", 4), ("truncation", 0.08)):
        rule = schema.get(key)
        if (not isinstance(rule, dict)
                or type(rule.get("minValue")) not in (int, float)
                or type(rule.get("maxValue")) not in (int, float)
                or not rule["minValue"] <= value <= rule["maxValue"]):
            raise CanaryError("live setting unsupported: " + key)
    for key, expected in (
            ("pasteurization", "ON"), ("unitHandling", "VERIFY"),
            ("nanHandling", "OFF"), ("maxTrade", "OFF")):
        rule = schema.get(key)
        options = rule.get("choices") if isinstance(rule, dict) else None
        choices = {item.get("value") for item in options if isinstance(item, dict)} if isinstance(options, list) else set()
        if expected not in choices:
            raise CanaryError("live setting unsupported: " + key)
    try:
        _validate_test_period("P0Y0M", schema.get("testPeriod"))
    except ValueError as exc:
        raise CanaryError("live testPeriod does not admit P0Y0M") from exc
    return {**SCOPE, "neutralization": "INDUSTRY", "decay": 4,
            "truncation": 0.08, "pasteurization": "ON",
            "unit_handling": "VERIFY", "nan_handling": "OFF",
            "language": "FASTEXPR", "visualization": False,
            "test_period": "P0Y0M", "max_trade": "OFF"}


def _verify_catalogs(fields, operators):
    if not isinstance(fields, list) or not isinstance(operators, list):
        raise CanaryError("complete field/operator catalogs are missing")
    catalog = {}
    for row in fields:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise CanaryError("field catalog contains invalid records")
        owner = row.get("dataset")
        owner = owner.get("id") if isinstance(owner, dict) else owner
        if (owner == DATASET and row.get("type") in {"MATRIX", "VECTOR"}
                and isinstance(row.get("description"), str) and row["description"]):
            catalog[row["id"]] = {"type": row["type"], "dataset": owner}
    regular = regular_operator_names(operators)
    result = validate(EXPRESSION, DATASET, catalog, regular,
                      operator_definitions=operators)
    if (FIELD not in catalog or catalog[FIELD]["type"] != "MATRIX"
            or result.get("verdict") != "PASS" or result.get("errors")
            or result.get("warnings") or result.get("operator_count") != 1
            or result.get("fields") != [FIELD]):
        raise CanaryError("expression is not supported by the full live catalogs")
    return {"field_count": len(fields), "regular_operator_count": len(regular)}


def _variant():
    return {"name": "canary_analyst_revision_ebitda_q1",
            "expression": EXPRESSION, "dataset": DATASET,
            "research": dict(RESEARCH)}


def _prepare(root, plan_id, gateway):
    digest = _verify_plan(root, plan_id)
    path = _canary_path(root, plan_id)
    if path.exists() or path.is_symlink():
        raise CanaryError("canary already prepared; inspect status, never replace it")
    gateway.call("set_case", case_key="canary_" + plan_id, run_id="single_" + plan_id)
    settings = _effective_settings(
        gateway.call("get_platform_setting_options", include_schema=True))
    gateway.call("validate_simulation_settings", settings=settings)
    catalogs = _verify_catalogs(
        gateway.call("get_datafields", **SCOPE, dataset_id=DATASET),
        gateway.call("get_operators"))
    gateway.call("check_simulation_cache", settings=settings,
                 expressions=[EXPRESSION])
    batch_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:8]
    record = {
        "schema_version": 1, "status": "PREPARED", "plan_id": plan_id,
        "plan_sha256": digest, "case_key": "canary_" + plan_id,
        "run_id": "single_" + plan_id, "batch_id": batch_id,
        "dataset_id": DATASET, "scope": dict(SCOPE), "settings": settings,
        "variants": [_variant()], "catalogs": catalogs, "max_posts": 1,
        "model_calls": 0, "alpha_submission": False, "auto_tags": False,
    }
    atomic_write_json(path, record)
    return {"status": "PREPARED_NO_POST", "plan_id": plan_id,
            "expression": EXPRESSION, "settings": settings,
            "field_count": catalogs["field_count"],
            "regular_operator_count": catalogs["regular_operator_count"],
            "record_path": str(path), "simulation_invoked": False}


def _execute(root, plan_id, gateway):
    path = _canary_path(root, plan_id)
    record = _read(path)
    _verify_plan(root, plan_id, record.get("plan_sha256"))
    if (record.get("schema_version") != 1
            or record.get("status") != "PREPARED"
            or record.get("plan_id") != plan_id
            or record.get("scope") != SCOPE
            or record.get("dataset_id") != DATASET
            or record.get("case_key") != "canary_" + plan_id
            or record.get("run_id") != "single_" + plan_id
            or not isinstance(record.get("batch_id"), str)
            or re.fullmatch(r"[0-9]{8}_[0-9]{6}_[a-f0-9]{8}", record["batch_id"]) is None
            or record.get("variants") != [_variant()]
            or record.get("max_posts") != 1 or record.get("model_calls") != 0
            or record.get("alpha_submission") is not False
            or record.get("auto_tags") is not False):
        raise CanaryError("canary was used or its pre-registered identity changed")
    gateway.call("set_case", case_key=record["case_key"], run_id=record["run_id"])
    settings = _effective_settings(
        gateway.call("get_platform_setting_options", include_schema=True))
    if settings != record.get("settings"):
        raise CanaryError("effective platform settings changed since pre-registration")
    gateway.call("validate_simulation_settings", settings=settings)
    catalogs = _verify_catalogs(
        gateway.call("get_datafields", **SCOPE, dataset_id=DATASET),
        gateway.call("get_operators"))
    if catalogs != record.get("catalogs"):
        raise CanaryError("field/operator catalog counts changed")
    gateway.call("check_simulation_cache", settings=settings,
                 expressions=[EXPRESSION])
    if StopSignals(root / "state").exists():
        raise CanaryError("stop flag forbids Simulation")
    # A crash at ANY later point leaves the attempt reserved permanently.
    atomic_write_json(path, {**record, "status": "ATTEMPT_RESERVED",
                             "reserved_at": datetime.now().astimezone().isoformat()})
    try:
        result = gateway.call(
            "simulate_canary_once", batch_id=record["batch_id"],
            dataset_id=DATASET, settings=settings, variants=[_variant()])
    except Exception as exc:
        raise CanaryError("outcome unknown; inspect durable ledger; DO NOT RETRY") from exc
    rows = result.get("results") if isinstance(result, dict) else None
    if (not isinstance(rows, list) or len(rows) != 1
            or not isinstance(rows[0], dict)
            or rows[0].get("expression") != EXPRESSION
            or rows[0].get("success") is not True
            or not isinstance(rows[0].get("alpha_id"), str)):
        raise CanaryError("Simulation readback unresolved; DO NOT RETRY")
    data = rows[0].get("is")
    if not isinstance(data, dict):
        data = {}
    summary = {
        "status": "COMPLETED_READBACK", "plan_id": plan_id,
        "alpha_id": rows[0]["alpha_id"], "expression": EXPRESSION,
        "metrics": {key: data.get(key) for key in
                    ("sharpe", "fitness", "turnover", "returns", "drawdown")},
        "runner": result.get("runner"),
        "model_invoked": False, "alpha_submission_invoked": False,
        "alpha_tags_modified": False,
    }
    atomic_write_json(path, {**record, "status": "COMPLETED_READBACK",
                             "completed_at": datetime.now().astimezone().isoformat(),
                             "alpha_id": rows[0]["alpha_id"]})
    return summary


def run_canary_command(args, root: Path, *, gateway_factory):
    try:
        path = _canary_path(root, args.plan_id)
        if args.command == "canary-review":
            from alpha_research.cli_canary_review import review_canary
            with SingleWriterLock(root / "state" / "agent.lock"):
                result = review_canary(root, args.plan_id)
            print(json.dumps(result, ensure_ascii=True, indent=2))
            return 0
        if args.command == "canary-status":
            record = _read(path)
            print(json.dumps({
                "status": record.get("status"), "plan_id": args.plan_id,
                "batch_id": record.get("batch_id"), "alpha_id": record.get("alpha_id"),
                "may_have_posted": record.get("status") != "PREPARED",
            }, ensure_ascii=True))
            return 0
        if marker_path(root).exists() or marker_path(root).is_symlink():
            raise CanaryError("mock workspace forbids live canary")
        if StopSignals(root / "state").exists():
            raise CanaryError("research stop flag is active")
        if not os.environ.get("BRAIN_CREDENTIALS_FILE"):
            raise CanaryError("BRAIN_CREDENTIALS_FILE is required")
        if args.command == "canary-execute" and args.allow_one_simulation is not True:
            raise CanaryError("explicit --allow-one-simulation is required")
        with SingleWriterLock(root / "state" / "agent.lock"):
            if args.command == "canary-execute":
                rec = _read(path)
                if rec.get("status") != "PREPARED":
                    raise CanaryError("attempt already reserved; DO NOT RETRY")
                _verify_plan(root, args.plan_id, rec.get("plan_sha256"))
            else:
                _verify_plan(root, args.plan_id)
            gateway = gateway_factory()
            try:
                gateway.start()
                result = (_prepare(root, args.plan_id, gateway)
                          if args.command == "canary-prepare"
                          else _execute(root, args.plan_id, gateway))
                print(json.dumps(result, ensure_ascii=True, indent=2))
                return 0
            finally:
                gateway.shutdown()
    except CanaryError as exc:
        print("canary blocked: " + str(exc), file=sys.stderr)
        return 2
    except Exception as exc:
        # No provider error body, credential path or account data leaves here.
        print("canary blocked: " + type(exc).__name__, file=sys.stderr)
        return 2
