"""Deterministic post-Canary root-cause triage from integrity-verified local evidence.

Only confirms observed metrics and platform RA check states. Research
hypotheses are explicitly labeled and cannot become Candidate/admission.
No Gateway, model, correlation, POST/PATCH or re-simulation is invoked.
"""
import json
from pathlib import Path

from alpha_research.cli_canary import CanaryError, _canary_path, _read
from alpha_research.cli_canary_review import review_canary, _read_value
from alpha_research.qualification.gates import RA_CHECK_NAMES
from alpha_research.workflows.regular.diagnosis import _signature_set
from alpha_research.state.store import atomic_write_json, file_sha256


def _ra_rows(details):
    is_data = details.get("is") if isinstance(details, dict) else None
    checks = is_data.get("checks") if isinstance(is_data, dict) else None
    if checks is None:
        return None
    if not isinstance(checks, list):
        raise CanaryError("platform RA check evidence has invalid shape")
    rows = []
    for item in checks:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise CanaryError("platform RA check record is malformed")
        name = item["name"]
        if name not in RA_CHECK_NAMES:
            continue
        value = str(item.get("result") or "").upper()
        if value not in {"PASS", "FAIL", "WARNING", "PENDING"}:
            raise CanaryError("platform RA check state is not readable")
        rows.append({"name": name, "result": value})
    if len({row["name"] for row in rows}) != len(rows):
        raise CanaryError("platform RA check contains duplicate check names")
    return sorted(rows, key=lambda row: row["name"])


def diagnose_canary(root, plan_id):
    """Require existing immutable review and verify all platform evidence first."""
    root = Path(root).resolve()
    review_path = _canary_path(root, plan_id).with_name(plan_id + "_review.json")
    saved_review = _read(review_path)
    review = review_canary(root, plan_id)
    if {key: value for key, value in review.items() if key != "review_path"} != saved_review:
        raise CanaryError("saved audit differs from verified platform evidence")
    if review.get("status") != "BELOW_THRESHOLD" or review.get("candidate_eligible") is not False:
        raise CanaryError("this failure-only diagnosis requires a rejected canary")
    canary = _read(_canary_path(root, plan_id))
    batch = canary.get("batch_id")
    base = (root / "state" / "platform" / "cases" / canary["case_key"] /
            "sessions" / canary["run_id"] / "batches" / (batch + "_production"))
    output = base.with_suffix(".json")
    if file_sha256(output) != review["platform_output_sha256"]:
        raise CanaryError("reviewed simulation evidence changed")
    raw = _read_value(output)
    if not isinstance(raw, list) or len(raw) != 1 or raw[0].get("alpha_id") != review["alpha_id"]:
        raise CanaryError("reviewed Alpha details are inconsistent")
    details = raw[0].get("details")
    checks = _ra_rows(details)
    if review["ra_check_state"] == "fail" and not checks:
        raise CanaryError("RA failure is reported without readable RA check evidence")
    failed_ra = [item["name"] for item in (checks or [])
                 if item["result"] in ("FAIL", "WARNING")]
    if review["ra_check_state"] == "fail" and not failed_ra:
        raise CanaryError("RA failure is reported without identifiable failed checks")
    if review["ra_check_state"] == "pass" and failed_ra:
        raise CanaryError("RA pass conflicts with failed RA check evidence")
    failures = review["failed_metrics"]
    readings = review["metrics"]
    gate = review["metric_thresholds"]
    signatures = _signature_set(
        {"failures": failures, "pending": review["unavailable_metrics"]},
        details, None, "USA", {"gate": gate},
    )
    # Mechanisms are plausible next research questions, not demonstrated causes.
    hypotheses = []
    if "sharpe" in failures:
        hypotheses.append({
            "key": "WEAK_PREDICTIVE_SIGNAL",
            "basis": ["sharpe"],
            "hypothesis": "Raw analyst revision levels may have weak cross-sectional predictive power.",
            "next_offline_action": "Design event-driven and relative-revision alternatives; validate field/operator signatures.",
        })
    if "fitness" in failures:
        hypotheses.append({
            "key": "WEAK_RISK_ADJUSTED_FITNESS",
            "basis": ["fitness"],
            "hypothesis": "Low fitness may reflect insufficient risk-adjusted performance for this turnover level.",
            "next_offline_action": "Compare signal rationale, frequency and exposure mechanics without new simulations.",
        })
    if "turnover" in failures and readings.get("turnover") is not None and readings["turnover"] > gate["turnover_max"]:
        hypotheses.append({
            "key": "HIGH_REBALANCING_FREQUENCY",
            "basis": ["turnover"],
            "hypothesis": "Daily rank updates may react excessively to analyst revision noise.",
            "next_offline_action": "Draft bounded time-series smoothing and event gating candidates for offline signature checks.",
        })
    result = {
        "schema_version": 1,
        "status": "DIAGNOSED_OFFLINE",
        "alpha_id": review["alpha_id"],
        "plan_id": plan_id,
        "research_stage": "failed_canary_triage",
        "candidate_eligible": False,
        "review_status": review["status"],
        "failed_metrics": failures,
        "ra_state": review["ra_check_state"],
        "failed_ra_checks": failed_ra,
        "ra_checks_available": checks is not None,
        "ra_checks": checks if checks is not None else [],
        "signatures": signatures,
        "hypotheses_not_proven": hypotheses,
        "correlation_allowed": False,
        "next_stage": "OFFLINE_RESEARCH_DESIGN_ONLY",
        "requires_new_simulation_authorization": True,
        "source_review_sha256": file_sha256(review_path),
        "source_simulation_output_sha256": review["platform_output_sha256"],
        "simulation_invoked": False,
        "model_invoked": False,
        "alpha_submission_invoked": False,
        "alpha_tags_modified": False,
    }
    dest = _canary_path(root, plan_id).with_name(plan_id + "_diagnosis.json")
    if dest.exists() or dest.is_symlink():
        if _read(dest) != result:
            raise CanaryError("existing diagnosis differs; original evidence must not be overwritten")
    else:
        atomic_write_json(dest, result)
    return {**result, "diagnosis_path": str(dest)}
