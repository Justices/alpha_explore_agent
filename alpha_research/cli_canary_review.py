"""Offline, immutable evidence + preliminary metric audit for one BRAIN Canary.

No BRAIN Gateway, model, Simulation, tags, or Alpha submissions are reachable
through this module. Candidate qualification remains in the full research
pipeline; this is a deterministic first-stage metric screen only.
"""
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path

from alpha_research.cli_canary import (
    CanaryError, DATASET, EXPRESSION, SCOPE, _read, _canary_path, _verify_plan, _variant
)
from alpha_research.integrations.brain.runtime import PlatformRuntime
from alpha_research.simulation.platform_cache import submission_key
from alpha_research.qualification.gates import ra_correlation_check_state
from alpha_research.state.store import atomic_write_json, file_sha256
from alpha_research.qualification.pool import policy_hash


def _read_value(path):
    if path.is_symlink() or not path.is_file():
        raise CanaryError("a required platform evidence file is missing or symlinked")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CanaryError("platform evidence file is unreadable") from exc


def _digest(path, recorded):
    if (not isinstance(recorded, str) or re.fullmatch(r"[a-f0-9]{64}", recorded) is None
            or file_sha256(path) != recorded):
        raise CanaryError("platform evidence hash mismatch")


def _number(value):
    if isinstance(value, bool) or type(value) not in (int, float) or not math.isfinite(value):
        return None
    return float(value)


def _compare_alpha_settings(submitted, readback):
    """Compare BRAIN Alpha detail settings with the POST-locked request.

    BRAIN can omit testPeriod from /alphas/{id} even when the Simulation
    request contained it. Treat only that single omission (for the pinned
    zero-length period) as *not echoed*, not as proof of a matching value.
    Every other missing or different requested setting is a hard failure.
    """
    if not isinstance(submitted, dict) or not isinstance(readback, dict):
        raise CanaryError("Alpha detail settings do not match pre-registration: invalid shape")
    unexpected = []
    not_echoed = []
    normalized = []
    for key, expected in submitted.items():
        if key not in readback:
            if key == "testPeriod" and expected == "P0Y0M":
                not_echoed.append(key)
            else:
                unexpected.append(key)
            continue
        actual = readback[key]
        # Explicit zero-duration equivalents, not arbitrary period coercion.
        if key == "testPeriod" and expected == "P0Y0M" and actual in ("P0Y", "P0M", "P0D"):
            normalized.append(key)
            continue
        # bool and numeric values must not match by Python's True == 1 rule.
        if (type(expected) is bool or type(actual) is bool):
            equal = type(expected) is type(actual) and expected == actual
        else:
            equal = actual == expected
        if not equal:
            unexpected.append(key)
    if unexpected:
        # Only fixed contract key names are displayed; never dump Alpha
        # detail objects, account data, or original HTTP response bodies.
        raise CanaryError("Alpha detail settings do not match pre-registration; keys: " +
                          ", ".join(sorted(unexpected)))
    return {
        "status": "PARTIAL_READBACK" if not_echoed else
                  ("NORMALIZED_READBACK" if normalized else "EXACT_READBACK"),
        "not_echoed": sorted(not_echoed),
        "normalized": sorted(normalized),
        "echo_verified_fields": sorted(set(submitted) - set(not_echoed)),
        "submitted_settings_verified_by_receipt_and_fingerprint": True,
    }


def review_canary(root, plan_id):
    """Verify the exact existing Simulation receipt; never initiate platform IO."""
    root = Path(root).resolve()
    canary = _read(_canary_path(root, plan_id))
    if canary.get("status") != "COMPLETED_READBACK":
        raise CanaryError("canary has no completed readback to audit")
    plan_sha = _verify_plan(root, plan_id, expected_digest=canary.get("plan_sha256"))
    case_key = "canary_" + plan_id
    run_id = "single_" + plan_id
    batch = canary.get("batch_id")
    alpha_id = canary.get("alpha_id")
    if (canary.get("schema_version") != 1
            or canary.get("case_key") != case_key or canary.get("run_id") != run_id
            or canary.get("plan_id") != plan_id or canary.get("dataset_id") != DATASET
            or canary.get("scope") != SCOPE
            or canary.get("variants") != [_variant()]
            or canary.get("max_posts") != 1
            or canary.get("auto_tags") is not False
            or canary.get("alpha_submission") is not False
            or not isinstance(batch, str)
            or re.fullmatch(r"[0-9]{8}_[0-9]{6}_[a-f0-9]{8}", batch) is None
            or not isinstance(alpha_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", alpha_id) is None):
        raise CanaryError("canary saved identity is inconsistent")
    base = (root / "state" / "platform" / "cases" / case_key /
            "sessions" / run_id / "batches" / (batch + "_production"))
    request_path = base.with_suffix(".request.json")
    receipt_path = base.with_suffix(".admission.json")
    progress_path = base.with_suffix(".progress.json")
    output_path = base.with_suffix(".json")
    marker_path = base.with_suffix(".post_started.json")
    request = _read(request_path)
    receipt = _read(receipt_path)
    progress = _read(progress_path)
    rows = _read_value(output_path)
    marker = _read(marker_path)
    if progress.get("status") != "COMPLETE":
        raise CanaryError("platform ledger is not complete")
    for path, key in ((request_path, "request_sha256"),
                      (receipt_path, "receipt_sha256"),
                      (output_path, "output_sha256")):
        _digest(path, progress.get(key))
    if (request.get("case_key") != case_key or request.get("run_id") != run_id
            or request.get("batch_id") != batch or request.get("label") != "production"
            or request.get("canary_no_tags") is not True
            or request.get("dataset_id") != DATASET
            or request.get("settings") != canary.get("settings")
            or request.get("variants") != [_variant()]
            or marker.get("request_sha256") != progress.get("request_sha256")
            or marker.get("receipt_sha256") != progress.get("receipt_sha256")
            or not isinstance(marker.get("submission_keys"), list)
            or len(marker["submission_keys"]) != 1
            or not isinstance(rows, list) or len(rows) != 1):
        raise CanaryError("platform pre-registration or single-request evidence differs")
    platform_settings = PlatformRuntime._platform_settings(canary["settings"])
    payload = {"type": "REGULAR", "settings": platform_settings,
               "regular": EXPRESSION}
    if marker["submission_keys"] != [submission_key(payload)]:
        raise CanaryError("recorded submission fingerprint does not match canary")
    admissions = receipt.get("field_admissions")
    if (receipt.get("dataset") != DATASET
            or receipt.get("variants") != [_variant()]
            or receipt.get("settings") != platform_settings
            or not isinstance(admissions, list) or len(admissions) != 1
            or admissions[0].get("passed") is not True
            or admissions[0].get("dataset") != DATASET
            or not isinstance(admissions[0].get("validation"), dict)
            or admissions[0]["validation"].get("verdict") != "PASS"
            or admissions[0]["validation"].get("fields") != [
                "analyst_revision_ratio_to_close_ebitda_q1"]):

        raise CanaryError("pre-backtest field admission receipt is inconsistent")
    row = rows[0]
    if (not isinstance(row, dict) or row.get("alpha_id") != alpha_id
            or row.get("expression") != EXPRESSION or row.get("success") is not True):
        raise CanaryError("Alpha readback does not match the pinned expression")
    details = row.get("details")
    if (not isinstance(details, dict) or details.get("id") != alpha_id
            or details.get("type") != "REGULAR"):
        raise CanaryError("Alpha detail readback identity missing")
    real_code = details.get("regular")
    real_code = real_code.get("code") if isinstance(real_code, dict) else real_code
    if real_code != EXPRESSION:
        raise CanaryError("Alpha detail expression does not match request")
    real_settings = details.get("settings")
    settings_verification = _compare_alpha_settings(platform_settings, real_settings)
    metrics = row.get("is")
    if not isinstance(metrics, dict):
        raise CanaryError("Alpha IS metrics are missing")
    if (not isinstance(details.get("is"), dict)
            or details["is"] != metrics):
        raise CanaryError("Alpha detail IS metrics differ from receipt")
    config_path = root / "config" / "nastu_quant.json"
    config = _read(config_path)
    gates = config.get("gate")
    if not isinstance(gates, dict):
        raise CanaryError("candidate metric gate policy is missing")
    thresholds = {}
    for k in ("sharpe_min", "fitness_min", "turnover_min", "turnover_max"):
        number = _number(gates.get(k))
        if number is None:
            raise CanaryError("candidate metric gate threshold is invalid")
        thresholds[k] = number
    if thresholds["turnover_min"] > thresholds["turnover_max"]:
        raise CanaryError("turnover gates are contradictory")
    values = {k: _number(metrics.get(k)) for k in
              ("sharpe", "fitness", "turnover", "returns", "drawdown")}
    failures = []
    pending = []
    if values["sharpe"] is None:
        pending.append("sharpe_unavailable")
    elif values["sharpe"] <= thresholds["sharpe_min"]:
        failures.append("sharpe")
    if values["fitness"] is None:
        pending.append("fitness_unavailable")
    elif values["fitness"] <= thresholds["fitness_min"]:
        failures.append("fitness")
    if values["turnover"] is None:
        pending.append("turnover_unavailable")
    elif not thresholds["turnover_min"] <= values["turnover"] <= thresholds["turnover_max"]:
        failures.append("turnover")
    ra_state = ra_correlation_check_state(details)
    status = ("BELOW_THRESHOLD" if failures else
              "FURTHER_AUDIT_REQUIRED")
    review = {
        "schema_version": 1,
        "status": status,
        "candidate_eligible": False,
        "alpha_id": alpha_id,
        "plan_id": plan_id,
        "expression": EXPRESSION,
        "dataset_id": DATASET,
        "scope": dict(SCOPE),
        "metrics": values,
        "metric_thresholds": thresholds,
        "failed_metrics": failures,
        "unavailable_metrics": pending,
        "ra_check_state": ra_state,
        "correlation_checked": False,
        "platform_evidence_verified": True,
        "settings_readback_verification": settings_verification,
        "plan_sha256": plan_sha,
        "config_policy_sha256": policy_hash(config),
        "platform_output_sha256": progress["output_sha256"],
        "model_invoked": False,
        "simulation_invoked": False,
        "alpha_submission_invoked": False,
        "alpha_tags_modified": False,
    }
    review_path = _canary_path(root, plan_id).with_name(plan_id + "_review.json")
    if review_path.exists() or review_path.is_symlink():
        existing = _read(review_path)
        if existing != review:
            raise CanaryError("previous canary review differs; never overwrite evidence")
    else:
        atomic_write_json(review_path, review)
    return {**review, "review_path": str(review_path)}
