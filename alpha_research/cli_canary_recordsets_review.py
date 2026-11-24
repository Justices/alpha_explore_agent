"""Deterministic offline interpretation of immutable BRAIN Alpha recordsets.

A verified Alpha-level recordset is NOT a raw DataField availability or
per-instrument weight export. Unknown types, units and shapes stay unresolved.
No BRAIN, Gateway, model, correlation, submission or Simulation is imported.
"""
import math
import re
from pathlib import Path

from alpha_research.cli_canary import CanaryError, EXPRESSION, SCOPE, _canary_path, _read
from alpha_research.cli_canary_field_decision import decide_fields_canary
from alpha_research.cli_canary_recordsets import RECORDSETS, _fingerprint
from alpha_research.state.store import atomic_write_json, file_sha256


def _schema_rows(name, data):
    if not isinstance(data, dict):
        return {"status": "SCHEMA_UNRESOLVED", "reason": "RECORDSET_NOT_OBJECT"}
    schema = data.get("schema")
    rows = data.get("records")
    if (not isinstance(schema, dict) or schema.get("name") != name
            or not isinstance(schema.get("properties"), list)
            or not isinstance(rows, list) or len(rows) > 50000):
        return {"status": "SCHEMA_UNRESOLVED", "reason": "UNVERIFIED_SCHEMA_OR_ROWS"}
    properties = schema["properties"]
    if not properties or len(properties) > 128:
        return {"status": "SCHEMA_UNRESOLVED", "reason": "UNSUPPORTED_PROPERTY_COUNT"}
    names = []
    for col in properties:
        if (not isinstance(col, dict) or not isinstance(col.get("name"), str)
                or re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,127}", col["name"]) is None
                or col["name"] in names):
            return {"status": "SCHEMA_UNRESOLVED", "reason": "INVALID_COLUMNS"}
        names.append(col["name"])
    for row in rows:
        if not isinstance(row, list) or len(row) != len(names):
            return {"status": "SCHEMA_UNRESOLVED", "reason": "ROW_COLUMN_MISMATCH"}
    return {"status": "SCHEMA_VERIFIED", "columns": names,
            "properties": properties, "rows": rows}


def _numeric_series(name, recordset, metric):
    parsed = _schema_rows(name, recordset)
    if parsed["status"] != "SCHEMA_VERIFIED":
        return parsed
    names = parsed["columns"]
    if metric not in names:
        return {"status": "SCHEMA_UNRESOLVED", "reason": "METRIC_COLUMN_NOT_LISTED",
                "columns": names, "record_count": len(parsed["rows"])}
    idx = names.index(metric)
    dtype = parsed["properties"][idx].get("type")
    if dtype not in {"integer", "decimal", "amount", "percent", "permyriad"}:
        return {"status": "SCHEMA_UNRESOLVED", "reason": "UNSUPPORTED_METRIC_UNIT",
                "raw_schema_type": dtype}
    values = []
    for row in parsed["rows"]:
        value = row[idx]
        if value is None:
            continue
        if (type(value) not in (int, float)
                or not math.isfinite(value)
                or (name == "coverage" and value < 0)):
            return {"status": "SCHEMA_UNRESOLVED", "reason": "INVALID_METRIC_VALUE"}
        values.append(value)
    if not values:
        return {"status": "SCHEMA_UNRESOLVED", "reason": "NO_NUMERIC_OBSERVATIONS"}
    try:
        mean = math.fsum(values) / len(values)
    except OverflowError:
        return {"status": "SCHEMA_UNRESOLVED", "reason": "NUMERIC_OVERFLOW"}
    if not math.isfinite(mean):
        return {"status": "SCHEMA_UNRESOLVED", "reason": "NUMERIC_OVERFLOW"}
    return {
        "status": "NUMERIC_SERIES_REVIEWED",
        "metric_name": metric,
        "observations": len(values),
        "missing_observations": len(parsed["rows"]) - len(values),
        "minimum_raw": min(values),
        "mean_raw": round(mean, 8),
        "maximum_raw": max(values),
        "raw_schema_type": dtype,
        "unit_converted": False,
        "coverage_fraction_inferred": False,
        "source_field_coverage_inferred": False,
        "portfolio_weight_concentration_inferred": False,
    }


def review_canary_recordsets(root, plan_id, *, evidence_path, snapshot_root=None):
    root = Path(root).resolve()
    decision = decide_fields_canary(root, plan_id, snapshot_root=snapshot_root)
    saved_decision_path = _canary_path(root, plan_id).with_name(
        plan_id + "_round3_decision.json")
    if (decision.get("simulation_authorized") is not False
            or decision.get("candidate_eligible") is not False):
        raise CanaryError("recordset review requires offline-only Canary decision")
    evidence_path = Path(evidence_path).resolve()
    expected_root = _canary_path(root, plan_id).parent.resolve()
    if (evidence_path.parent != expected_root
            or re.fullmatch(re.escape(plan_id) + r"_recordsets_[a-f0-9]{16}\\.json",
                            evidence_path.name) is None
            or evidence_path.is_symlink()):
        raise CanaryError("recordset evidence must be a pinned local immutable file")
    envelope = _read(evidence_path)
    if _fingerprint(envelope)[:16] not in evidence_path.name:
        raise CanaryError("recordset evidence fingerprint does not match file identity")
    report = envelope.get("report")
    payloads = envelope.get("payloads")
    if (not isinstance(report, dict) or not isinstance(payloads, dict)
            or report.get("plan_id") != plan_id
            or report.get("source_alpha_id") != decision["source_alpha_id"]
            or report.get("scope") != SCOPE
            or report.get("expression") != EXPRESSION
            or report.get("source_field_decision_sha256") != file_sha256(saved_decision_path)
            or report.get("source_provenance_verified") is not True
            or report.get("simulation_authorized") is not False
            or report.get("candidate_eligible") is not False
            or not payloads):
        raise CanaryError("recordset envelope is not a verified Canary readback")
    rows = report.get("recordsets")
    if not isinstance(rows, dict):
        raise CanaryError("recordset capture metadata missing")
    reviewed = {}
    for name, data in payloads.items():
        info = rows.get(name)
        if (name not in RECORDSETS or not isinstance(info, dict)
                or info.get("status") != "CAPTURED_SCHEMA_REVIEW_REQUIRED"
                or info.get("sha256") != _fingerprint(data)):
            raise CanaryError("recordset data or checksum changed after capture")
        if name in ("coverage", "turnover"):
            reviewed[name] = _numeric_series(
                name, data, "instruments" if name == "coverage" else "turnover")
        else:
            tabular = _schema_rows(name, data)
            reviewed[name] = (
                {"status": "GROUP_SCHEMA_REVIEW_REQUIRED",
                 "columns": tabular["columns"],
                 "record_count": len(tabular["rows"]),
                 "numeric_values_not_interpreted": True}
                if tabular["status"] == "SCHEMA_VERIFIED" else tabular)
    output = {
        "schema_version": 1,
        "status": ("ALPHA_RECORDSET_NUMERIC_EVIDENCE_REVIEWED_OFFLINE"
                   if any(r["status"] == "NUMERIC_SERIES_REVIEWED"
                          for r in reviewed.values()) else
                   "ALPHA_RECORDSET_SCHEMA_REVIEW_REQUIRED"),
        "plan_id": plan_id,
        "source_alpha_id": decision["source_alpha_id"],
        "recordset_evidence_sha256": file_sha256(evidence_path),
        "recordset_evidence_fingerprint": _fingerprint(envelope),
        "source_field_decision_sha256": file_sha256(saved_decision_path),
        "scope": dict(SCOPE), "metrics": reviewed,
        "alpha_recordset_not_raw_field_or_position_evidence": True,
        "concentration_root_cause_established": False,
        "candidate_eligible": False, "simulation_authorized": False,
        "model_invoked": False, "simulation_invoked": False,
        "alpha_submission_invoked": False, "alpha_tags_modified": False,
        "next_stage": "REVIEW_ALPHA_RECORDSET_DIAGNOSTICS_AND_MISSING_RAW_EVIDENCE",
    }
    dest = _canary_path(root, plan_id).with_name(
        plan_id + "_recordsets_review_" + _fingerprint(envelope)[:16] + ".json")
    if dest.exists() or dest.is_symlink():
        if _read(dest) != output:
            raise CanaryError("existing recordset review differs; no overwrite")
    else:
        atomic_write_json(dest, output)
    return {**output, "recordset_review_path": str(dest)}
