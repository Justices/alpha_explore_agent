"""Evidence-bound Round 3 field-substitution decision; no network or Simulation.

Catalog coverage is not time-series coverage and never proves Alpha portfolio
coverage. Static expression validity does not validate economic equivalence.
This step reports only grounded catalog differences and the evidence required
to attribute observed CONCENTRATED_WEIGHT / HIGH_TURNOVER failures.
"""
from collections import Counter
from pathlib import Path

from alpha_research.cli_canary import CanaryError, FIELD, _canary_path, _read
from alpha_research.cli_canary_field_review import review_fields_canary
from alpha_research.cli_canary_shortlist import _coverage
from alpha_research.state.paths import CATALOG_SNAPSHOT_ROOT
from alpha_research.state.store import atomic_write_json, file_sha256


# Internal research materiality screen: 5 percentage points of absolute
# catalog coverage difference. NOT a WorldQuant/BRAIN qualifying threshold.
RESEARCH_MATERIAL_GAIN = 0.05


def _semantic_group(record):
    facts = record["semantics"]
    field_id = record["field_id"]
    measure = facts["catalog_measure_clue"]
    direction = facts["directionality_clue"]
    if measure == "revision_count":
        return "COUNT_" + direction.upper()
    if measure == "revision_ratio":
        return ("RATIO_TO_CONSENSUS" if "to_consensus" in field_id
                else "OTHER_REVISION_RATIO")
    if "revision_value" in field_id:
        return "REVISION_VALUE_UNVERIFIED_UNITS"
    return "OTHER_UNVERIFIED_MEASURE"


def _candidate_fact(record, source_coverage):
    coverage = _coverage(record["catalog_facts"]["coverage"])
    if coverage is None:
        raise CanaryError("alternative coverage is not a finite catalog fraction")
    difference = round(coverage - source_coverage, 6)
    if difference != record["coverage_delta_vs_source"]:
        raise CanaryError("alternative coverage delta differs from verified field review")
    if record.get("ready_for_simulation") is not False:
        raise CanaryError("unexpected substitute simulation eligibility")
    if record.get("same_measure_as_source_observed") is not False:
        raise CanaryError("unexpected substitute semantic equivalence")
    expressions = record.get("candidate_expressions")
    if not isinstance(expressions, list) or len(expressions) != 2:
        raise CanaryError("alternative expression contract count changed")
    if any(e.get("simulation_authorized") is not False or
           e.get("substitution_approved") is not False for e in expressions):
        raise CanaryError("substitute expression unexpectedly authorized")
    return {
        "field_id": record["field_id"],
        "semantic_family_clue": _semantic_group(record),
        "description": record["description"],
        "catalog_coverage": coverage,
        "absolute_coverage_gain": difference,
        "gain_percentage_points": round(100 * difference, 4),
        "date_coverage": _coverage(record["catalog_facts"].get("date_coverage")),
        "zero_fraction": _coverage(record["catalog_facts"].get("zero_fraction")),
        "observed_q1": "q1" in record["semantics"].get("forecast_quarter_clues", []),
        "static_pass_count": sum(e["status"] == "STATIC_PASS" for e in expressions),
        "static_blocked_count": sum(e["status"] == "STATIC_BLOCKED" for e in expressions),
        "economic_equivalence_verified": False,
        "catalog_coverage_equivalent_to_alpha_coverage": False,
        "automatic_substitution_allowed": False,
        "simulation_authorized": False,
    }


def decide_fields_canary(root, plan_id, *, snapshot_root=None):
    root = Path(root).resolve()
    snapshot_root = Path(snapshot_root or CATALOG_SNAPSHOT_ROOT)
    # Validate every upstream receipt and frozen field+operator snapshot.
    reviewed = review_fields_canary(root, plan_id, snapshot_root=snapshot_root)
    review_path = _canary_path(root, plan_id).with_name(
        plan_id + "_round3_field_review.json")
    stored = _read(review_path)
    if stored != {k: v for k, v in reviewed.items()
                  if k != "field_review_path"}:
        raise CanaryError("Round 3 field review differs from verified evidence")
    if (reviewed.get("status") not in {
            "FIELDS_REVIEW_REQUIRED", "NO_COMPARABLE_FIELDS_IN_SHORTLIST"}
            or reviewed.get("simulation_authorized") is not False
            or reviewed.get("candidate_eligible") is not False
            or reviewed.get("ready_for_simulation") is not False
            or reviewed.get("source_field", {}).get("field_id") != FIELD):
        raise CanaryError("Round 3 field review does not support offline decision")

    source_coverage = _coverage(reviewed["source_field"].get("coverage"))
    if source_coverage is None:
        raise CanaryError("original source field lacks a readable catalog coverage")
    records = reviewed.get("alternatives")
    if not isinstance(records, list):
        raise CanaryError("Round 3 alternative field list is not readable")
    if reviewed.get("counts", {}).get("alternatives_shown") != len(records):
        raise CanaryError("Round 3 alternative field count changed")
    seen = set()
    compared = []
    for item in records:
        if not isinstance(item, dict) or not isinstance(item.get("field_id"), str):
            raise CanaryError("alternative catalog row has an invalid identity")
        key = item["field_id"]
        if not key or key == FIELD or key in seen:
            raise CanaryError("duplicate or source field in alternatives")
        seen.add(key)
        compared.append(_candidate_fact(item, source_coverage))
    compared.sort(key=lambda v: (-v["catalog_coverage"], v["field_id"]))
    maximal = max((item["absolute_coverage_gain"] for item in compared), default=None)
    gain = ("NO_RELATED_FIELDS" if maximal is None else
            "NO_MATERIAL_COVERAGE_GAIN" if maximal < RESEARCH_MATERIAL_GAIN
            else "MATERIAL_COVERAGE_GAIN_REQUIRES_SEMANTIC_REVIEW")
    counts = dict(sorted(Counter(item["semantic_family_clue"]
                                 for item in compared).items()))
    # A coverage difference alone never approves a replacement or Simulation.
    status = ("NO_COVERAGE_BENEFIT_FROM_FIELD_SUBSTITUTION" if
              gain == "NO_MATERIAL_COVERAGE_GAIN" else
              "NO_RELATED_FIELDS_REQUIRES_NEW_DATA_DISCOVERY" if
              gain == "NO_RELATED_FIELDS" else
              "FIELD_SUBSTITUTION_REVIEW_REQUIRED")
    report = {
        "schema_version": 1,
        "status": status,
        "plan_id": plan_id,
        "source_alpha_id": reviewed["source_alpha_id"],
        "source_field_review_sha256": file_sha256(review_path),
        "source_field_snapshot_sha256": reviewed["source_field_snapshot_sha256"],
        "source_operator_snapshot_sha256": reviewed["source_operator_snapshot_sha256"],
        "source_field": {
            "field_id": FIELD,
            "catalog_coverage": source_coverage,
            "date_coverage": reviewed["source_field"].get("date_coverage"),
            "zero_fraction": reviewed["source_field"].get("zero_fraction"),
            "concentration_failure_observed": reviewed["source_field"].get(
                "observed_weight_concentration_fail"),
        },
        "catalog_coverage_comparison": {
            "alternative_count": len(compared),
            "max_coverage": max((x["catalog_coverage"] for x in compared), default=None),
            "max_absolute_gain": maximal,
            "max_gain_percentage_points": (
                round(100 * maximal, 4) if maximal is not None else None),
            "materiality_research_heuristic_absolute": RESEARCH_MATERIAL_GAIN,
            "materiality_heuristic_is_platform_rule": False,
            "coverage_gain_verdict": gain,
            "catalog_coverage_is_not_actual_alpha_coverage": True,
            "date_coverage_is_not_cross_sectional_nonnull_rate": True,
        },
        "semantic_family_counts": counts,
        "field_comparisons": compared,
        "research_decision": {
            "replacement_approved": False,
            "retest_all_12_expressions": False,
            "recommendation": (
                "DO_NOT_SWITCH_FIELD_ON_COVERAGE_GROUNDS" if
                gain == "NO_MATERIAL_COVERAGE_GAIN"
                else "REVIEW_FIELD_SEMANTICS_AND_MISSINGNESS_BEFORE_SWITCH"),
            "original_candidate_still_below_threshold": True,
            "concentration_mechanism_identified": False,
            "field_value_distribution_available": False,
            "portfolio_weight_distribution_available": False,
            "ranking_means_evidence_priority_not_return_expectation": True,
            "next_comparison": (
                "Measure nonnull signal availability and sector/industry weight "
                "concentration for the original analyst10 field before further tests."),
        },
        "outstanding_evidence": [
            {
                "evidence_id": "PER_DATE_EFFECTIVE_COVERAGE",
                "required_columns": ["date", "instrument_id", "field_value"],
                "required_computation": "non-null instrument fraction by date, versus full TOP3000 universe",
                "available_from_catalog_only": False,
            },
            {
                "evidence_id": "MISSINGNESS_BY_GROUP",
                "required_columns": ["date", "instrument_id", "sector", "field_value"],
                "required_computation": "non-null ratio and long zero-history gaps by date and sector",
                "available_from_catalog_only": False,
            },
            {
                "evidence_id": "POSITION_CONCENTRATION",
                "required_columns": ["date", "instrument_id", "portfolio_weight"],
                "required_computation": "max weight, top-5 weight share, HHI, effective holdings and sector concentration",
                "available_from_catalog_only": False,
            },
            {
                "evidence_id": "SIGNAL_UPDATE_FREQUENCY",
                "required_columns": ["date", "instrument_id", "field_value"],
                "required_computation": "fraction of stocks updated each date and unchanged-value run lengths",
                "available_from_catalog_only": False,
            },
        ],
        "provenance_verified": True,
        "simulation_authorized": False,
        "ready_for_simulation": False,
        "candidate_eligible": False,
        "correlation_allowed": False,
        "model_invoked": False,
        "simulation_invoked": False,
        "alpha_submission_invoked": False,
        "alpha_tags_modified": False,
        "next_stage": "OBTAIN_DISTRIBUTION_EVIDENCE_OR_EXPLORE_OTHER_DATASETS",
    }
    dest = _canary_path(root, plan_id).with_name(plan_id + "_round3_decision.json")
    if dest.exists() or dest.is_symlink():
        if _read(dest) != report:
            raise CanaryError("existing field-substitution decision differs; never overwrite")
    else:
        atomic_write_json(dest, report)
    return {**report, "decision_path": str(dest)}
