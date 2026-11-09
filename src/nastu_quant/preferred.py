"""Second-stage selection for already-qualified Alpha Candidates.

Candidate qualification remains owned by the existing RA/correlation gate.
This module never upgrades a non-Candidate and never submits an Alpha.
"""
import math


DEFAULT_PREFERRED_POLICY = {
    "sharpe_min": 1.25,
    "fitness_min": 1.0,
    "turnover_min": 0.01,
    "turnover_max": 0.70,
    "weight_max": 0.10,
    "require_sub_universe_pass": True,
}


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _check(details, name):
    block = details.get("is") if isinstance(details, dict) else None
    rows = block.get("checks") if isinstance(block, dict) else None
    if not isinstance(rows, list):
        return None
    matches = [row for row in rows if isinstance(row, dict) and row.get("name") == name]
    return matches[0] if len(matches) == 1 else None


def _weight(details):
    block = details.get("is") if isinstance(details, dict) else None
    if not isinstance(block, dict):
        return None
    for key in ("max_weight", "maxWeight", "weight"):
        value = _finite(block.get(key))
        if value is not None:
            return abs(value)
    check = _check(details, "CONCENTRATED_WEIGHT")
    if isinstance(check, dict):
        value = _finite(check.get("value"))
        if value is not None:
            return abs(value)
    return None


def evaluate_preferred(details, *, candidate_status, config=None):
    """Evaluate optional preferred quality only after Candidate qualification."""
    policy = dict(DEFAULT_PREFERRED_POLICY)
    configured = (config or {}).get("preferred")
    if isinstance(configured, dict):
        policy.update(configured)
    is_data = details.get("is") if isinstance(details, dict) else None
    is_data = is_data if isinstance(is_data, dict) else {}
    observed = {
        "sharpe": _finite(is_data.get("sharpe")),
        "fitness": _finite(is_data.get("fitness")),
        "turnover": _finite(is_data.get("turnover")),
        "weight": _weight(details),
        "drawdown": _finite(is_data.get("drawdown")),
        "coverage": _finite(is_data.get("coverage")),
    }
    result = {
        "policy_version": "preferred-v1",
        "candidate_status": candidate_status,
        "status": "NOT_CANDIDATE",
        "qualified": False,
        "metrics": observed,
        "thresholds": policy,
        "failures": [],
        "pending": [],
        "informational_metrics": ["drawdown", "coverage"],
    }
    if candidate_status != "CANDIDATE":
        return result

    checks = (
        ("sharpe", observed["sharpe"], lambda x: x >= policy["sharpe_min"]),
        ("fitness", observed["fitness"], lambda x: x >= policy["fitness_min"]),
        ("turnover", observed["turnover"],
         lambda x: policy["turnover_min"] <= x <= policy["turnover_max"]),
        ("weight", observed["weight"], lambda x: x <= policy["weight_max"]),
    )
    for name, value, predicate in checks:
        if value is None:
            result["pending"].append(name + "_unavailable")
        elif not predicate(value):
            result["failures"].append(name)

    if policy.get("require_sub_universe_pass", True):
        check = _check(details, "LOW_SUB_UNIVERSE_SHARPE")
        if not isinstance(check, dict):
            result["pending"].append("sub_universe_check_unavailable")
        elif str(check.get("result") or "").upper() != "PASS":
            state = str(check.get("result") or "").upper()
            if state == "FAIL":
                result["failures"].append("sub_universe")
            else:
                result["pending"].append("sub_universe_check_unsettled")

    result["failures"] = sorted(set(result["failures"]))
    result["pending"] = sorted(set(result["pending"]))
    if result["failures"]:
        result["status"] = "NOT_PREFERRED"
    elif result["pending"]:
        result["status"] = "PENDING_PREFERRED"
    else:
        result["status"] = "PREFERRED"
        result["qualified"] = True
    return result
