"""Offline research shortlist: coverage first, no performance claims.

Validates the immutable Round 2 design against account-bound catalogs, then
compares cautiously related EBITDA-revision fields with observed coverage.
The shortlist is NOT a Simulation intent, Candidate, or Alpha submission.
No Gateway, browser, model, correlation, or BRAIN requests are permitted.
"""
import math
from pathlib import Path

from alpha_research.cli_canary import CanaryError, DATASET, FIELD, _canary_path, _read
from alpha_research.cli_canary_design import design_canary, _catalogs
from alpha_research.state.paths import CATALOG_SNAPSHOT_ROOT
from alpha_research.state.store import atomic_write_json, file_sha256


RESEARCH_COVERAGE_SCREEN = 0.50  # research triage heuristic, NOT a BRAIN platform gate
PRIORITY = (
    ("R2_SMOOTH_10", "Isolate revision smoothing and potential turnover impact."),
    ("R2_RELATIVE_20", "Test unusual revisions while investigating zero-variance and sparse-history risk."),
    ("R2_PERSISTENCE_5", "Complex nested windows can compound missingness and turnover."),
)


def _coverage(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return float(value) if 0 <= value <= 1 else None


def _related_ebitda_revision_fields(rows, original_coverage):
    """Identify *possible*, never proven, same-source comparators."""
    candidates = []
    for row in rows:
        if not isinstance(row, dict) or row.get("id") == FIELD:
            continue
        owner = row.get("dataset")
        owner = owner.get("id") if isinstance(owner, dict) else owner
        if owner != DATASET or row.get("type") != "MATRIX":
            continue
        desc = row.get("description")
        if not isinstance(desc, str) or not desc.strip():
            continue
        identifier = row.get("id")
        if not isinstance(identifier, str):
            continue
        joined = (identifier + " " + desc).lower()
        if not ("ebitda" in joined and
                ("revision" in joined or "revisions" in joined)):
            continue
        cov = _coverage(row.get("coverage"))
        if cov is None:
            continue
        # No inference that fields have equivalent semantics, only a grounded
        # lexical match and a catalog-measured coverage difference.
        candidates.append({
            "id": identifier,
            "type": "MATRIX",
            "description": desc,
            "coverage": cov,
            "coverage_delta_vs_original": (
                round(cov - original_coverage, 6) if original_coverage is not None
                else None),
            "semantic_equivalence_verified": False,
            "static_expression_verified": False,
            "substitution_approved": False,
        })
    candidates.sort(key=lambda row: (-row["coverage"], row["id"]))
    return candidates[:12], len(candidates)


def shortlist_canary(root, plan_id, *, snapshot_root=None):
    root = Path(root).resolve()
    design_path = _canary_path(root, plan_id).with_name(plan_id + "_round2_design.json")
    stored = _read(design_path)
    snapshot_root = Path(snapshot_root or CATALOG_SNAPSHOT_ROOT)
    verified = design_canary(root, plan_id, snapshot_root=snapshot_root)
    if stored != {k: v for k, v in verified.items() if k != "design_path"}:
        raise CanaryError("Round 2 design differs from verified source evidence")
    if (verified.get("status") != "DESIGNED_OFFLINE"
            or not isinstance(verified.get("source_alpha_id"), str)
            or not verified["source_alpha_id"]
            or verified.get("candidate_eligible") is not False
            or verified.get("simulation_authorized") is not False):
        raise CanaryError("a complete, rejected, unapproved design is required")
    fields, operators = _catalogs(root, plan_id, snapshot_root=snapshot_root)
    expected = verified["catalog_evidence"]
    if (fields["sha256"] != expected.get("fields_sha256")
            or operators["sha256"] != expected.get("operators_sha256")):
        raise CanaryError("Round 2 catalog digests changed since static verification")
    facts = verified.get("original_field_facts")
    if not isinstance(facts, dict) or facts.get("id") != FIELD or facts.get("type") != "MATRIX":
        raise CanaryError("source MATRIX field facts missing")
    current_cov = _coverage(facts.get("coverage"))
    if current_cov is None:
        coverage_state = "UNAVAILABLE"
    elif current_cov < RESEARCH_COVERAGE_SCREEN:
        coverage_state = "HIGH_COVERAGE_RISK"
    else:
        coverage_state = "RESEARCH_REVIEW_REQUIRED"
    proposals = verified.get("proposals")
    if not isinstance(proposals, list) or len(proposals) != len(PRIORITY):
        raise CanaryError("unexpected Round 2 design list")
    by_id = {p.get("proposal_id"): p for p in proposals if isinstance(p, dict)}
    if set(by_id) != {name for name, _ in PRIORITY}:
        raise CanaryError("Round 2 design identities changed")
    ranking = []
    for rank, (proposal_id, rationale) in enumerate(PRIORITY, 1):
        candidate = by_id[proposal_id]
        static_pass = candidate.get("static_status") == "STATIC_PASS"
        ranking.append({
            "research_priority": rank,
            "proposal_id": proposal_id,
            "expression": candidate["expression"],
            "static_status": candidate["static_status"],
            "prioritization_basis": rationale,
            "observed_field_coverage": current_cov,
            "coverage_risk": coverage_state,
            "effective_alpha_coverage_verified": False,
            "turnover_improvement_verified": False,
            "concentration_improvement_verified": False,
            "ready_for_simulation": False,
            "unapproved_reason": (
                "STATIC_BLOCKED" if not static_pass else
                "COVERAGE_RISK_AND_INDEPENDENT_AUTHORIZATION_REQUIRED"
                if coverage_state == "HIGH_COVERAGE_RISK" else
                "FURTHER_RESEARCH_AND_INDEPENDENT_AUTHORIZATION_REQUIRED"),
        })
    alternatives, total_related = _related_ebitda_revision_fields(fields["rows"], current_cov)
    report = {
        "schema_version": 1,
        "status": "PRIORITIZED_OFFLINE_COVERAGE_REVIEW",
        "plan_id": plan_id,
        "source_alpha_id": verified["source_alpha_id"],
        "source_round2_design_sha256": file_sha256(design_path),
        "source_fields_sha256": fields["sha256"],
        "source_operators_sha256": operators["sha256"],
        "coverage_screen": {
            "original_field": FIELD,
            "catalog_coverage": current_cov,
            "uncovered_fraction": (round(1 - current_cov, 6) if current_cov is not None else None),
            "risk": coverage_state,
            "heuristic_threshold": RESEARCH_COVERAGE_SCREEN,
            "threshold_is_platform_gate": False,
            "estimated_actual_portfolio_coverage": None,
            "coverage_is_not_position_coverage": True,
        },
        "research_order_not_performance_rank": ranking,
        "related_datafields": {
            "matched_total_with_readable_coverage": total_related,
            "shown": len(alternatives),
            "candidates": alternatives,
            "semantics_must_be_manually_reviewed": True,
            "substitutions_not_authorized": True,
        },
        "required_before_new_simulation": [
            "Explain field missingness and analyst-update frequency by date and sector",
            "Evaluate signal score sparsity and concentrated-weight mechanism",
            "Manually review any alternative DataField economic equivalence",
            "Create a new independent pre-registration and grant for one specific Simulation",
        ],
        "simulation_authorized": False,
        "simulation_invoked": False,
        "model_invoked": False,
        "correlation_checked": False,
        "candidate_eligible": False,
        "alpha_submission_invoked": False,
        "next_stage": "COVERAGE_AND_FIELD_SEMANTICS_RESEARCH_REVIEW",
    }
    dest = _canary_path(root, plan_id).with_name(plan_id + "_round2_shortlist.json")
    if dest.exists() or dest.is_symlink():
        if _read(dest) != report:
            raise CanaryError("prior shortlist differs; do not overwrite original research evidence")
    else:
        atomic_write_json(dest, report)
    return {**report, "shortlist_path": str(dest)}
