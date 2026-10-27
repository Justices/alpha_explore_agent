"""Deterministic, explanation-only diagnosis of settled gate evidence."""
import hashlib
import json
from pathlib import Path

from .validator import Parser, walk


class DiagnosisError(RuntimeError):
    pass


CHECK_SIGNATURES = {
    "LOW_SHARPE": "S_SHARPE",
    "LOW_FITNESS": "S_FITNESS",
    "HIGH_TURNOVER": "S_TURNOVER_HI",
    "LOW_TURNOVER": "S_TURNOVER_LO",
    "CONCENTRATED_WEIGHT": "S_CONCENTRATED",
    "LOW_2Y_SHARPE": "S_2Y",
    "IS_LADDER_SHARPE": "S_LADDER",
    "LOW_GLB_AMER_SHARPE": "S_REGION",
    "LOW_GLB_APAC_SHARPE": "S_REGION",
    "LOW_GLB_EMEA_SHARPE": "S_REGION",
    "LOW_ASI_JPN_SHARPE": "S_REGION",
    "LOW_SUB_UNIVERSE_SHARPE": "S_SUBUNIV",
    "LOW_ROBUST_UNIVERSE_SHARPE": "S_ROBUST",
    "LOW_ROBUST_UNIVERSE_RETURNS": "S_ROBUST",
    "LOW_AFTER_COST_ILLIQUID_UNIVERSE_SHARPE": "S_AFTERCOST",
    "LOW_INVESTABILITY_CONSTRAINED_SHARPE": "S_INVEST",
    "LOW_RETURNS": "S_RETURNS",
}


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def _signature_set(verdict, details, check_payload, region, config):
    failures = set(verdict.get("failures") or [])
    names = {item.split(":", 1)[1] for item in failures
             if item.startswith("ra:")}
    is_data = (details.get("is") or {}) if isinstance(details, dict) else {}
    check_rows = is_data.get("checks")
    if not isinstance(check_rows, list) and isinstance(check_payload, dict):
        check_rows = check_payload.get("checks")
    if isinstance(check_rows, dict):
        check_rows = check_rows.get("checks")
    for row in check_rows or []:
        if isinstance(row, dict) and str(row.get("result", "")).upper() in {"FAIL", "WARNING"}:
            names.add(row.get("name"))

    signatures = set()
    gate_config = config.get("gate") or {}
    metric_names = {
        "sharpe": "S_SHARPE", "fitness": "S_FITNESS",
        "turnover": None, "two_year_sharpe": "S_2Y",
    }
    for failure in failures:
        if failure in metric_names and metric_names[failure]:
            signatures.add(metric_names[failure])
    for name in names:
        signature = CHECK_SIGNATURES.get(name)
        if signature:
            signatures.add(signature)
    if "turnover" in failures:
        turnover = is_data.get("turnover")
        try:
            signatures.add("S_TURNOVER_HI" if float(turnover) >
                           gate_config.get("turnover_max", 0.30)
                           else "S_TURNOVER_LO")
        except (TypeError, ValueError):
            pass

    sharpe = is_data.get("sharpe")
    try:
        main_sharpe_passed = (float(sharpe) >= gate_config.get("china_sharpe_min", 2.08)
                              if str(region or "").upper() == "CHN" else
                              float(sharpe) > gate_config.get("sharpe_min", 1.58))
    except (TypeError, ValueError):
        main_sharpe_passed = False
    if set(verdict.get("pending") or []).intersection(
            {"region_threshold_unavailable", "sharpe_unavailable"}):
        main_sharpe_passed = False
    if main_sharpe_passed and {"S_FITNESS", "S_2Y"} <= signatures:
        signatures.add("C_DECAY")
    if main_sharpe_passed and {"S_TURNOVER_HI", "S_AFTERCOST"} <= signatures:
        signatures.add("C_COST")
    if main_sharpe_passed and signatures.intersection({"S_SUBUNIV", "S_ROBUST"}):
        signatures.add("C_COVER")
    if {"S_LADDER", "S_2Y"} <= signatures:
        signatures.add("C_SINGLE_YEAR")
    order = ("S_SHARPE", "S_FITNESS", "S_TURNOVER_HI", "S_TURNOVER_LO",
             "S_CONCENTRATED", "S_2Y", "S_LADDER", "S_REGION", "S_SUBUNIV",
             "S_ROBUST", "S_AFTERCOST", "S_INVEST", "S_RETURNS",
             "C_DECAY", "C_COST", "C_COVER", "C_SINGLE_YEAR")
    return [item for item in order if item in signatures]


def _structure(expression, validation):
    try:
        root = Parser(expression).parse()
    except Exception as exc:
        raise DiagnosisError(f"validated expression cannot be parsed for diagnosis: {exc}") from exc
    calls = [node for node, _ in walk(root) if node.kind == "call"]
    windows = []
    for call in calls:
        for arg in call.args:
            if arg.kind == "num":
                windows.append(arg.value)
    ending = root.value if root.kind == "call" else None
    return {"fields": validation.get("fields", []),
            "operator_count": validation.get("operator_count"),
            "operators": [call.value for call in calls],
            "ending_operator": ending, "numeric_arguments": windows}


def diagnose_batch(batch_dir: Path, prereg: dict, gate: dict, readback: dict,
                   config: dict, remedies_lookup) -> dict:
    """Build diagnosis from saved validation and closed gate evidence only."""
    variants = prereg.get("variants")
    validation = prereg.get("validation")
    if not isinstance(variants, list) or not variants or not isinstance(validation, dict):
        raise DiagnosisError("pre-registration lacks expression validation evidence")
    expressions = [row.get("expression") for row in variants if isinstance(row, dict)]
    if len(expressions) != len(variants) or any(not isinstance(expr, str) for expr in expressions):
        raise DiagnosisError("pre-registration variants are unreadable")
    if any((validation.get(expr) or {}).get("verdict") != "PASS" for expr in expressions):
        raise DiagnosisError("structurally invalid expression is not eligible for diagnosis")

    raw_gate = (Path(batch_dir) / "gate.json").read_bytes()
    diagnosis_id = hashlib.sha256(
        str(Path(batch_dir).resolve()).encode("utf-8") + raw_gate).hexdigest()[:20]
    proposal = prereg.get("proposal") or {}
    mechanism_tags = proposal.get("mechanism_tags") or ["unclassified"]
    if isinstance(mechanism_tags, str):
        mechanism_tags = [mechanism_tags]
    field_generation_tags = proposal.get("field_generation_tags") or []
    if isinstance(field_generation_tags, str):
        field_generation_tags = [field_generation_tags]
    if not field_generation_tags:
        field_generation_tags = sorted({field_id for expr in expressions
                                        for field_id in (validation[expr].get("fields") or [])})
    fields = {row.get("id"): row for row in prereg.get("fields") or []
              if isinstance(row, dict) and row.get("id")}
    catalog_keys = ("type", "coverage", "zeroFraction", "updateFrequency",
                    "units", "median", "distribution")
    probe_by_field = {}
    probe_path = Path(batch_dir) / "probe_evidence.json"
    if probe_path.exists():
        try:
            probe_payload = json.loads(probe_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise DiagnosisError("saved probe evidence is unreadable") from exc
        for probe in probe_payload.get("observations", []):
            if not isinstance(probe, dict) or not probe.get("field_id"):
                continue
            facts = probe_by_field.setdefault(probe["field_id"], {})
            for observation in probe.get("observations", []):
                if not isinstance(observation, dict) or not observation.get("dimension"):
                    continue
                facts.setdefault(observation["dimension"], []).append({
                    "observation": observation.get("observation"),
                    "source": observation.get("source"),
                    "limit": observation.get("limit")})

    alphas = {}
    alpha_ids = list(gate)
    if len(alpha_ids) != len(expressions):
        raise DiagnosisError("gate Alpha count does not match validated pre-registration")
    for alpha_id, expression in zip(alpha_ids, expressions):
        verdict = gate[alpha_id]
        evidence = readback.get(alpha_id)
        if not isinstance(verdict, dict) or not isinstance(evidence, dict):
            raise DiagnosisError(f"gate/read-back evidence missing for {alpha_id}")
        details = evidence.get("details") or {}
        signatures = _signature_set(
            verdict, details, evidence.get("checks"),
            (prereg.get("scope") or {}).get("region"), config)
        field_facts = {"field_ids": validation[expression].get("fields", []),
                       "field_types": {field_id: fields.get(field_id, {}).get("type")
                                       for field_id in validation[expression].get("fields", [])},
                       "dataset_id": prereg.get("dataset_id"),
                       "field_catalog": {
                           field_id: {key: fields[field_id][key] for key in catalog_keys
                                      if key in fields.get(field_id, {}) and
                                      fields[field_id][key] is not None}
                           for field_id in validation[expression].get("fields", [])},
                       "probe_observations": {
                           field_id: probe_by_field.get(field_id, {})
                           for field_id in validation[expression].get("fields", [])}}
        remedy_sets = remedies_lookup(signatures, field_facts)
        active_remedy = (prereg.get("remedy_reference") or {}).get("remedy_id")
        if active_remedy:
            remedy_sets["verified"] = [item for item in remedy_sets.get("verified", [])
                                        if item.get("remedy_id") != active_remedy]
        alphas[alpha_id] = {
            "diagnosis_id": diagnosis_id, "status": verdict.get("status"),
            "failures": verdict.get("failures", []), "pending": verdict.get("pending", []),
            "signatures": signatures,
            "structure": _structure(expression, validation[expression]),
            "field_facts": field_facts,
            "remedy_candidates": remedy_sets.get("verified", []),
            "historical_remedies": remedy_sets.get("historical", []),
            "recommendation": ("no_known_remedy" if signatures and not remedy_sets.get("verified")
                               else "pending_model"),
        }
    body = {"schema_version": 1, "diagnosis_id": diagnosis_id,
            "batch": str(Path(batch_dir).resolve()),
            "objective": prereg.get("objective"),
            "dataset_id": prereg.get("dataset_id"),
            "scope": {key: (prereg.get("scope") or {}).get(key)
                      for key in ("region", "delay", "universe")},
            "mechanism_tags": mechanism_tags,
            "field_generation_tags": field_generation_tags,
            "alphas": alphas, "model_diagnosis": {"status": "pending"}}
    body["content_sha256"] = hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()
    return body
