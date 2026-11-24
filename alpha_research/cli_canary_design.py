"""Offline next-round design from a rejected BRAIN analyst10 Canary.

Consumes immutable prior diagnosis + complete account-bound field and operator
snapshots. Creates only proposed static expressions and a durable study plan.
No Gateway, model calls, Simulation POST, correlation checks or Alpha submission.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from alpha_research.cli_canary import CanaryError, DATASET, FIELD, SCOPE, _canary_path, _read
from alpha_research.cli_canary_diagnose import diagnose_canary
from alpha_research.expressions.validator import regular_operator_names, validate, Parser, walk
from alpha_research.state.paths import CATALOG_SNAPSHOT_ROOT
from alpha_research.state.store import atomic_write_json, file_sha256


# Hypotheses, not expected performance improvements. No field arithmetic or
# banned add/multiply/trade_when operators; platform signatures are checked
# against the REAL stored operator catalog before a design is admitted.
PROPOSALS = (
    {
        "proposal_id": "R2_SMOOTH_10",
        "failure_signatures": ["S_TURNOVER_HI", "S_FITNESS"],
        "expression": f"rank(ts_mean({FIELD}, 10))",
        "mechanism": "Smooth the estimate-revision ratio over ten observations before daily cross-sectional ranking.",
        "economic_rationale": "A persistent revision direction may be more informative than isolated analyst updates.",
        "primary_hypothesis": "Reduce response to short-lived revision noise; not guaranteed to lower turnover.",
        "falsification": "Signal remains weak or rank turnover stays excessive after smoothing.",
        "concentration_warning": "Time smoothing does not guarantee unconcentrated weights or nonmissing coverage.",
    },
    {
        "proposal_id": "R2_RELATIVE_20",
        "failure_signatures": ["S_SHARPE", "S_2Y"],
        "expression": f"rank(ts_zscore({FIELD}, 20))",
        "mechanism": "Normalize current revision intensity against a stock's own trailing twenty observations.",
        "economic_rationale": "An unusual forecast revision can carry more information than routine updates.",
        "primary_hypothesis": "Relative revisions may be more informative across analysts and securities than raw levels.",
        "falsification": "Relative surprises show no stable predictive relationship or have poor history coverage.",
        "concentration_warning": "Low temporal variance can create missing or extreme scores; investigate coverage.",
    },
    {
        "proposal_id": "R2_PERSISTENCE_5",
        "failure_signatures": ["S_SHARPE", "S_TURNOVER_HI", "S_2Y"],
        "expression": f"rank(ts_delta(ts_mean({FIELD}, 5), 5))",
        "mechanism": "Rank the change in the five-observation rolling revision signal.",
        "economic_rationale": "A sustained improvement in earnings expectations may precede a price adjustment.",
        "primary_hypothesis": "Revision acceleration, not its absolute level, may capture new information.",
        "falsification": "Difference signal is noisier, fails predictive stability or increases turnover.",
        "concentration_warning": "Nested rolling windows require more temporal coverage; validate the missing-data rate.",
    },
)


def _load_snapshot(root, relative, snapshot_root, required_meta, rows_key):
    """Require exact pinned-case reference and SHA-256 of local snapshot bytes."""
    reference = root / relative.with_suffix(".ref.json")
    if reference.is_symlink() or not reference.is_file():
        raise CanaryError("catalog reference is unavailable in original Canary case")
    ref = _read(reference)
    dest = ref.get("snapshot_ref")
    sha = ref.get("sha256")
    if (not isinstance(dest, str) or not isinstance(sha, str)
            or len(sha) != 64):
        raise CanaryError("catalog reference has no verifiable SHA-256")
    path = Path(dest)
    # Ref paths may be absolute on the originating Windows host; no arbitrary
    # external path or symlink escapes may be followed by the offline process.
    resolved_root = Path(snapshot_root).resolve()
    if (path.is_symlink() or path.resolve() != (resolved_root /
            ("data_fields/USA/1/TOP3000/analyst10.json" if rows_key == "fields"
             else "operator/operators.json")).resolve()
            or not path.resolve().is_relative_to(resolved_root)):
        raise CanaryError("catalog reference points outside the expected snapshot location")
    if not path.is_file() or file_sha256(path) != sha:
        raise CanaryError("catalog snapshot differs from original Canary reference")
    snapshot = _read(path)
    meta = snapshot.get("metadata")
    rows = snapshot.get(rows_key)
    if (not isinstance(meta, dict) or not isinstance(rows, list)
            or meta.get("complete") is not True
            or type(meta.get("count")) is not int
            or meta["count"] != len(rows) or len(rows) < 1
            or any(meta.get(k) != v for k, v in required_meta.items())):
        raise CanaryError("catalog snapshot is not complete or exact-scope")
    if (not isinstance(meta.get("account_namespace"), str)
            or not meta["account_namespace"]):
        raise CanaryError("catalog snapshot account identity is unavailable")
    try:
        fetched = datetime.fromisoformat(meta["fetched_at"])
        if fetched.utcoffset() is None:
            raise ValueError("naive datetime")
        delta = datetime.now(timezone.utc) - fetched
        if delta < timedelta(0) or delta > timedelta(days=7):
            raise ValueError("outdated snapshot")
    except (KeyError, TypeError, ValueError) as exc:
        raise CanaryError("catalog snapshot is expired or lacks valid timestamps") from exc
    key = "id" if rows_key == "fields" else "name"
    if (any(not isinstance(row, dict) or not isinstance(row.get(key), str)
            or not row[key] for row in rows)
            or len({row[key] for row in rows}) != len(rows)):
        raise CanaryError("catalog snapshot contains duplicate or invalid records")
    return {"rows": rows, "metadata": meta, "sha256": sha}


def _catalogs(root, plan_id, *, snapshot_root):
    # PlatformRuntime.set_case stores catalog *.ref.json under run_root,
    # not case_root: cases/<case_key>/sessions/<run_id>/.
    # Pin both components to the previously verified Canary identity; never
    # search sibling sessions or fall back to an unaudited reference file.
    session = (root / "state" / "platform" / "cases" / ("canary_" + plan_id) /
               "sessions" / ("single_" + plan_id))
    fields = _load_snapshot(
        session, Path("fields/USA/1/TOP3000/analyst10.json"), snapshot_root,
        {"region": "USA", "delay": 1, "universe": "TOP3000",
         "instrument_type": "EQUITY", "dataset": DATASET,
         "source": "https://api.worldquantbrain.com/data-fields"}, "fields")
    operators = _load_snapshot(
        session, Path("catalogs/operators.json"), snapshot_root,
        {"source": "https://api.worldquantbrain.com/operators"}, "operators")
    if fields["metadata"]["account_namespace"] != operators["metadata"]["account_namespace"]:
        raise CanaryError("field and operator snapshot account identities differ")
    return fields, operators


def _static_proposal(proposal, catalog, operators, regular):
    verdict = validate(
        proposal["expression"], DATASET, catalog, regular,
        operator_definitions=operators,
    )
    calls = sorted({node.value for node, _ in walk(Parser(proposal["expression"]).parse())
                    if node.kind == "call"})
    # PASS with an unverifiable live signature is NOT admitted.
    admitted = (verdict.get("verdict") == "PASS" and not verdict.get("errors")
                and not verdict.get("warnings") and verdict.get("fields") == [FIELD])
    return {
        **proposal,
        "operator_names": calls,
        "field_ids": verdict.get("fields", []),
        "operator_count": verdict.get("operator_count"),
        "static_status": "STATIC_PASS" if admitted else "STATIC_BLOCKED",
        "static_validation": verdict,
        "simulation_authorized": False,
        "candidate_eligible": False,
        "submission_authorized": False,
    }


def design_canary(root, plan_id, *, snapshot_root=None):
    """Offline-only and deterministic; always reject changed stored evidence."""
    root = Path(root).resolve()
    diagnosis_path = _canary_path(root, plan_id).with_name(plan_id + "_diagnosis.json")
    prior_diagnosis = _read(diagnosis_path)
    diagnosis = diagnose_canary(root, plan_id)
    result_from_diagnosis = {k: v for k, v in diagnosis.items() if k != "diagnosis_path"}
    if prior_diagnosis != result_from_diagnosis:
        raise CanaryError("saved diagnosis changed since last verified review")
    if (diagnosis.get("status") != "DIAGNOSED_OFFLINE"
            or diagnosis.get("candidate_eligible") is not False
            or diagnosis.get("ra_state") != "fail"
            or diagnosis.get("next_stage") != "OFFLINE_RESEARCH_DESIGN_ONLY"
            or diagnosis.get("correlation_allowed") is not False
            or not {"S_SHARPE", "S_FITNESS", "S_TURNOVER_HI", "S_CONCENTRATED", "S_2Y"}.issubset(
                set(diagnosis.get("signatures") or []))):
        raise CanaryError("this pinned round-two design requires the documented failed RA signatures")
    canary = _read(_canary_path(root, plan_id))
    if (canary.get("status") != "COMPLETED_READBACK"
            or canary.get("alpha_id") != diagnosis.get("alpha_id")):
        raise CanaryError("source Canary was not completed consistently")
    snapshot_root = Path(snapshot_root or CATALOG_SNAPSHOT_ROOT)
    fields, ops = _catalogs(root, plan_id, snapshot_root=snapshot_root)
    old_counts = canary.get("catalogs")
    regular = regular_operator_names(ops["rows"])
    if (not isinstance(old_counts, dict)
            or len(fields["rows"]) != old_counts.get("field_count")
            or len(regular) != old_counts.get("regular_operator_count")):
        raise CanaryError("live-admitted catalog counts differ from frozen canary")
    catalog = {}
    field_facts = {}
    for item in fields["rows"]:
        owner = item.get("dataset")
        owner = owner.get("id") if isinstance(owner, dict) else owner
        if owner == DATASET and item.get("type") in {"MATRIX", "VECTOR"}:
            catalog[item["id"]] = {"type": item["type"], "dataset": owner}
            if item["id"] == FIELD:
                field_facts = {
                    k: item.get(k) for k in
                    ("id", "type", "description", "coverage", "zeroFraction",
                     "updateFrequency", "units") if item.get(k) is not None}
    if FIELD not in catalog or catalog[FIELD]["type"] != "MATRIX":
        raise CanaryError("original MATRIX field absent in complete exact-scope snapshot")
    candidate_rows = [_static_proposal(row, catalog, ops["rows"], regular)
                      for row in PROPOSALS]
    status = ("DESIGNED_OFFLINE" if any(
        row["static_status"] == "STATIC_PASS" for row in candidate_rows)
        else "BLOCKED_NO_SIGNATURE_VERIFIED_CANDIDATES")
    report = {
        "schema_version": 1,
        "status": status,
        "research_stage": "round_two_offline_static_design",
        "plan_id": plan_id,
        "source_alpha_id": diagnosis["alpha_id"],
        "dataset_id": DATASET,
        "scope": dict(SCOPE),
        "reference_expression": f"rank({FIELD})",
        "source_diagnosis_sha256": file_sha256(diagnosis_path),
        "source_simulation_output_sha256": diagnosis["source_simulation_output_sha256"],
        "catalog_evidence": {
            "fields_sha256": fields["sha256"],
            "operators_sha256": ops["sha256"],
            "field_count": len(fields["rows"]),
            "operator_count": len(ops["rows"]),
            "regular_operator_count": len(regular),
            "source_account_verified_equal": True,
            "catalog_ttl_valid": True,
        },
        "original_field_facts": field_facts,
        "failure_signatures": diagnosis["signatures"],
        "concentration_risk": {
            "observed_ra_failure": "CONCENTRATED_WEIGHT",
            "hypotheses_not_proven": [
                "Sparse analyst forecast revisions may produce uneven coverage.",
                "A few unusually strong scores may dominate portfolio weights.",
                "Platform truncation may not eliminate concentration from missing-data effects.",
            ],
            "verification_needed": ["field_coverage", "zero_fraction", "weight_distribution",
                                    "missing_data_behavior"],
            "no_concentration_remedy_assumed": True,
        },
        "proposals": candidate_rows,
        "counts": {
            "total": len(candidate_rows),
            "static_pass": sum(row["static_status"] == "STATIC_PASS" for row in candidate_rows),
            "static_blocked": sum(row["static_status"] == "STATIC_BLOCKED" for row in candidate_rows),
        },
        "simulation_authorized": False,
        "model_invoked": False,
        "simulation_invoked": False,
        "alpha_submission_invoked": False,
        "correlation_checked": False,
        "candidate_eligible": False,
        "next_stage": "MANUAL_RESEARCH_REVIEW_AND_INDEPENDENT_SIMULATION_AUTHORIZATION",
    }
    path = _canary_path(root, plan_id).with_name(plan_id + "_round2_design.json")
    if path.exists() or path.is_symlink():
        if _read(path) != report:
            raise CanaryError("existing round-two design changed; never overwrite source evidence")
    else:
        atomic_write_json(path, report)
    return {**report, "design_path": str(path)}
