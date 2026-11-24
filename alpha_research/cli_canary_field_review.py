"""Evidence-bound, offline-only field diagnostics following a failed Canary.

Verifies (or produces) the immutable Round 2 shortlist, compares complete
account-scoped catalog facts, and statically checks alternative expressions.
Lexical field similarity is never treated as semantic equivalence, performance
evidence, or permission to run a BRAIN Simulation or submit an Alpha.
"""
from pathlib import Path

from alpha_research.cli_canary import CanaryError, DATASET, FIELD, _canary_path, _read
from alpha_research.cli_canary_design import _catalogs
from alpha_research.cli_canary_shortlist import (
    RESEARCH_COVERAGE_SCREEN, _coverage, shortlist_canary,
)
from alpha_research.expressions.validator import regular_operator_names, validate
from alpha_research.state.paths import CATALOG_SNAPSHOT_ROOT
from alpha_research.state.store import atomic_write_json, file_sha256


def _semantic_facts(field_id, description):
    """Conservative human-readable clues; no automatic economic equivalence."""
    tokens = (field_id + " " + description).lower()
    if "ratio" in tokens:
        measure = "revision_ratio"
    elif "count" in tokens or field_id.endswith("_cnt"):
        measure = "revision_count"
    elif "score" in tokens:
        measure = "score"
    elif "flag" in tokens:
        measure = "flag"
    else:
        measure = "other_or_ambiguous"
    if "increase" in field_id or "upward" in tokens:
        direction = "upward_only"
    elif "decrease" in field_id or "downward" in tokens:
        direction = "downward_only"
    else:
        direction = "mixed_or_unspecified"
    horizons = tuple(h for h in ("q1", "q2", "q3", "q4")
                     if h in field_id.lower().split("_"))
    return {
        "catalog_measure_clue": measure,
        "directionality_clue": direction,
        "forecast_quarter_clues": list(horizons),
        "economic_semantic_equivalence_to_source": "UNVERIFIED",
        "requires_human_examination": True,
    }


def _numeric_catalog_fact(value):
    return _coverage(value)


def _static_expression(field_id, all_fields, operators, names, *,
                       smoothing=False):
    expression = (f"rank(ts_mean({field_id}, 10))" if smoothing
                  else f"rank({field_id})")
    catalog = {
        row["id"]: {
            "dataset": (row["dataset"].get("id") if isinstance(row.get("dataset"), dict)
                        else row.get("dataset")),
            "type": row.get("type"),
        }
        for row in all_fields
        if isinstance(row, dict) and isinstance(row.get("id"), str)
        and isinstance(row.get("dataset"), (dict, str))
    }
    result = validate(expression, DATASET, catalog, names,
                      operator_definitions=operators)
    accepted = (result.get("verdict") == "PASS"
                and not result.get("errors") and not result.get("warnings")
                and result.get("fields") == [field_id])
    return {
        "expression": expression,
        "status": "STATIC_PASS" if accepted else "STATIC_BLOCKED",
        "operator_count": result.get("operator_count"),
        "errors": result.get("errors", []),
        "warnings": result.get("warnings", []),
        "simulation_authorized": False,
        "substitution_approved": False,
    }


def review_fields_canary(root, plan_id, *, snapshot_root=None):
    root = Path(root).resolve()
    snapshot_root = Path(snapshot_root or CATALOG_SNAPSHOT_ROOT)

    # Revalidate full upstream provenance. Creates the shortlist locally if
    # the previous stage was not explicitly invoked; never rewrites it.
    shortlist = shortlist_canary(root, plan_id, snapshot_root=snapshot_root)
    shortlist_path = _canary_path(root, plan_id).with_name(
        plan_id + "_round2_shortlist.json")
    if _read(shortlist_path) != {k: v for k, v in shortlist.items()
                               if k != "shortlist_path"}:
        raise CanaryError("shortlist differs from verified source evidence")
    if (shortlist.get("status") != "PRIORITIZED_OFFLINE_COVERAGE_REVIEW"
            or shortlist.get("simulation_authorized") is not False
            or shortlist.get("candidate_eligible") is not False):
        raise CanaryError("Round 2 shortlist is not an approved offline source")

    fields, ops = _catalogs(root, plan_id, snapshot_root=snapshot_root)
    if (fields["sha256"] != shortlist.get("source_fields_sha256")
            or ops["sha256"] != shortlist.get("source_operators_sha256")):
        raise CanaryError("source field/operator snapshots changed after shortlist")
    names = regular_operator_names(ops["rows"])
    original = next((r for r in fields["rows"]
                     if isinstance(r, dict) and r.get("id") == FIELD), None)
    if not isinstance(original, dict) or original.get("type") != "MATRIX":
        raise CanaryError("original MATRIX source field is absent")
    cov = _coverage(original.get("coverage"))
    if cov != shortlist["coverage_screen"].get("catalog_coverage"):
        raise CanaryError("source field coverage differs from validated shortlist")

    proposed = shortlist.get("related_datafields", {}).get("candidates")
    if not isinstance(proposed, list) or len(proposed) > 12:
        raise CanaryError("shortlist alternative catalog is invalid")
    by_id = {row["id"]: row for row in fields["rows"]
             if isinstance(row, dict) and isinstance(row.get("id"), str)}
    records = []
    for suggested in proposed:
        if not isinstance(suggested, dict):
            raise CanaryError("shortlist candidate has invalid structure")
        key = suggested.get("id")
        live = by_id.get(key)
        if (not isinstance(key, str) or not isinstance(live, dict)
                or live.get("type") != "MATRIX"
                or live.get("description") != suggested.get("description")
                or _coverage(live.get("coverage")) != suggested.get("coverage")):
            raise CanaryError("shortlist candidate is not backed by same catalog")
        owner = live.get("dataset")
        owner = owner.get("id") if isinstance(owner, dict) else owner
        if owner != DATASET or key == FIELD:
            raise CanaryError("shortlist candidate is not a distinct same-dataset MATRIX")
        evidence = _semantic_facts(key, live["description"])
        raw_metrics = {
            "coverage": _coverage(live.get("coverage")),
            "date_coverage": _numeric_catalog_fact(live.get("dateCoverage")),
            "zero_fraction": _numeric_catalog_fact(live.get("zeroFraction")),
            "update_frequency": live.get("updateFrequency")
                                if isinstance(live.get("updateFrequency"), str) else None,
            "units": live.get("units") if isinstance(live.get("units"), str) else None,
        }
        records.append({
            "field_id": key,
            "dataset_id": DATASET,
            "description": live["description"],
            "catalog_facts": raw_metrics,
            "coverage_delta_vs_source": suggested.get("coverage_delta_vs_original"),
            "semantics": evidence,
            "same_measure_as_source_observed": False,
            "candidate_expressions": [
                _static_expression(key, fields["rows"], ops["rows"], names),
                _static_expression(key, fields["rows"], ops["rows"], names,
                                   smoothing=True),
            ],
            "requires_manual_field_approval": True,
            "ready_for_simulation": False,
        })
    counts = {
        "alternatives_shown": len(records),
        "expressions_checked": 2 * len(records),
        "static_pass": sum(x["status"] == "STATIC_PASS" for row in records
                           for x in row["candidate_expressions"]),
        "static_blocked": sum(x["status"] == "STATIC_BLOCKED" for row in records
                              for x in row["candidate_expressions"]),
    }
    status = ("FIELDS_REVIEW_REQUIRED" if records
              else "NO_COMPARABLE_FIELDS_IN_SHORTLIST")
    source_semantics = _semantic_facts(FIELD, original.get("description") or "")
    result = {
        "schema_version": 1,
        "status": status,
        "plan_id": plan_id,
        "source_alpha_id": shortlist["source_alpha_id"],
        "source_shortlist_sha256": file_sha256(shortlist_path),
        "source_field_snapshot_sha256": fields["sha256"],
        "source_operator_snapshot_sha256": ops["sha256"],
        "source_field": {
            "field_id": FIELD,
            "coverage": cov,
            "measure_clues": source_semantics,
            "date_coverage": _numeric_catalog_fact(original.get("dateCoverage")),
            "zero_fraction": _numeric_catalog_fact(original.get("zeroFraction")),
            "catalog_coverage_is_not_position_coverage": True,
            "observed_weight_concentration_fail": True,
            "weight_distribution_inspected": False,
            "temporal_missingness_inspected": False,
            "concentration_mechanism_determined": False,
        },
        "alternatives": records,
        "counts": counts,
        "research_controls": {
            "heuristic_coverage_floor": RESEARCH_COVERAGE_SCREEN,
            "coverage_floor_is_platform_policy": False,
            "lexical_similarity_not_economic_equivalence": True,
            "static_validation_not_economic_success": True,
            "replacement_fields_approved": False,
            "missing_weight_and_temporal_evidence": [
                "per-date field nonnull coverage",
                "sector and instrument missingness",
                "portfolio weight concentration distribution",
                "signal update intervals",
            ],
        },
        "next_stage": "MANUAL_SEMANTIC_AND_DISTRIBUTION_EVIDENCE_REVIEW",
        "ready_for_simulation": False,
        "candidate_eligible": False,
        "simulation_authorized": False,
        "model_invoked": False,
        "simulation_invoked": False,
        "correlation_checked": False,
        "alpha_submission_invoked": False,
        "alpha_tags_modified": False,
    }
    path = _canary_path(root, plan_id).with_name(
        plan_id + "_round3_field_review.json")
    if path.exists() or path.is_symlink():
        if _read(path) != result:
            raise CanaryError("previous Round 3 field audit differs; never overwrite evidence")
    else:
        atomic_write_json(path, result)
    return {**result, "field_review_path": str(path)}
