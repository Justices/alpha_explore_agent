"""Gate logic (FR-10, AGENT_DESIGN.md §3.3): alpha lifecycle states and
the structurally ordered RA Gate -> correlation -> Candidate verdict.

The order is enforced by the state machine: correlation may only be
checked from state RA_PASSED. The Gateway calls
`assert_correlation_allowed` before forwarding any check_correlation
command — a second, independent layer (acceptance A2).
"""
import math

from .config import DEFAULT_CONFIG
from .ppa import evaluate_ppa

ALPHA_STATES = {
    "CREATED", "SIMULATED", "CLASSIFIED", "BELOW_THRESHOLD",
    "RA_PASSED", "RA_FAILED", "CORR_PASSED", "CORR_REJECTED",
    "CANDIDATE", "TECH_ERROR",
}

# Legal transitions of the alpha lifecycle (design §3.3).
TRANSITIONS = {
    "CREATED": {"SIMULATED"},
    "SIMULATED": {"CLASSIFIED", "TECH_ERROR"},
    "CLASSIFIED": {"BELOW_THRESHOLD", "RA_PASSED", "RA_FAILED"},
    "RA_PASSED": {"CORR_PASSED", "CORR_REJECTED"},
    "CORR_PASSED": {"CANDIDATE"},
    "BELOW_THRESHOLD": set(),
    "RA_FAILED": set(),
    "CORR_REJECTED": set(),
    "CANDIDATE": set(),
    "TECH_ERROR": set(),
}


class GateOrderError(RuntimeError):
    """Raised when a gate step is attempted out of order."""


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, set())


def transition(alpha: dict, dst: str) -> dict:
    src = alpha["state"]
    if not can_transition(src, dst):
        raise GateOrderError(f"illegal alpha transition {src} -> {dst}")
    alpha["state"] = dst
    return alpha


def assert_correlation_allowed(alpha: dict) -> None:
    """Precondition for any correlation check: RA must have passed."""
    if (alpha.get("state") != "RA_PASSED" or alpha.get("ra_fail_count") != 0
            or alpha.get("ra_readable") is not True):
        raise GateOrderError(
            f"correlation check forbidden in state {alpha.get('state')}: "
            "readable RA evidence with zero failures is required")


def sharpe_gate(sharpe: float, cfg: dict) -> bool:
    return sharpe is not None and sharpe >= cfg["gate"]["sharpe_min"]


def ra_gate(ra_fail_count: int) -> bool:
    """RA Gate passes iff the fail count is exactly zero."""
    return ra_fail_count == 0


def correlation_verdict(prod_corr, self_corr, pairwise_corrs, cfg: dict):
    """Current research rule: prod/self <= limit, pairwise < limit."""
    g = cfg["gate"]
    reasons = []
    for name, value in (("prod", prod_corr), ("self", self_corr)):
        number = _finite(value)
        limit = g[name + "_corr_max"]
        if number is None or not -1 <= number <= 1 or abs(number) > limit:
            reasons.append(f"{name}_corr {value} not within +/-{limit}")
    for other_id, value in (pairwise_corrs or {}).items():
        number = _finite(value)
        if (number is None or not -1 <= number <= 1
                or abs(number) >= g["pairwise_corr_max"]):
            reasons.append(
                f"pairwise vs {other_id}: abs({value}) not < {g['pairwise_corr_max']}")
    return (not reasons, reasons)


RA_CHECK_NAMES = frozenset({
    "HIGH_TURNOVER", "LOW_TURNOVER", "LOW_FITNESS", "LOW_RETURNS", "LOW_SHARPE",
    "LOW_GLB_AMER_SHARPE", "LOW_GLB_APAC_SHARPE", "LOW_GLB_EMEA_SHARPE",
    "LOW_ASI_JPN_SHARPE", "IS_LADDER_SHARPE", "LOW_2Y_SHARPE",
    "LOW_SUB_UNIVERSE_SHARPE", "LOW_ROBUST_UNIVERSE_SHARPE",
    "LOW_AFTER_COST_ILLIQUID_UNIVERSE_SHARPE",
    "LOW_INVESTABILITY_CONSTRAINED_SHARPE", "LOW_ROBUST_UNIVERSE_RETURNS",
    "CONCENTRATED_WEIGHT",
})
SUPPORTED_REGIONS = frozenset({"ASI", "CHN", "DEU", "EUR", "GBR", "GLB",
                               "HKG", "IND", "KOR", "USA"})


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def terminal_correlation_check(checks, name):
    """Return a single readable terminal platform check, without guessing duplicates."""
    if not isinstance(checks, list):
        return None
    matches = [row for row in checks if isinstance(row, dict) and row.get("name") == name]
    if len(matches) != 1:
        return None
    check = matches[0]
    value = _finite(check.get("value"))
    if (str(check.get("result") or "").upper() not in {"PASS", "FAIL", "WARNING"}
            or value is None or not -1 <= value <= 1):
        return None
    return check


def local_self_correlation_state(evidence, limit):
    """Classify a local prefilter without treating missing comparisons as zero."""
    threshold = _finite(limit)
    if (threshold is None or not 0 <= threshold <= 1 or not isinstance(evidence, dict)
            or _finite(evidence.get("limit")) != threshold
            or evidence.get("window_years") != 4
            or type(evidence.get("comparison_count")) is not int
            or evidence["comparison_count"] < 1):
        return "pending"
    value = _finite(evidence.get("value"))
    if value is None or not -1 <= value <= 1:
        return "pending"
    if abs(value) > threshold:
        return "fail"
    if evidence.get("status") == "pass" and evidence.get("complete") is True:
        return "pass"
    return "pending"


def _checks(detail, check_payload):
    is_data = detail.get("is") if isinstance(detail, dict) else None
    source = is_data.get("checks") if isinstance(is_data, dict) else None
    if source is None and isinstance(check_payload, dict):
        source = check_payload.get("checks")
    if not isinstance(source, list):
        return None
    if any(not isinstance(row, dict) or not isinstance(row.get("name"), str)
           or not row["name"] for row in source):
        return None
    rows = [row for row in source if isinstance(row, dict)
            and row.get("name") in RA_CHECK_NAMES]
    return rows or None


def ra_correlation_check_state(detail):
    """Classify whether readable RA checks permit correlation diagnostics."""
    rows = _checks(detail, None)
    if rows is None:
        return "unavailable"
    states = [str(row.get("result") or "").upper() for row in rows]
    if any(state not in {"PASS", "PENDING", "FAIL", "WARNING"} for state in states):
        return "unavailable"
    if any(state in {"FAIL", "WARNING"} for state in states):
        return "fail"
    return "pending" if "PENDING" in states else "pass"


def _two_year(is_data, rows):
    values = []
    if "sharpe_2y" in is_data:
        values.append(_finite(is_data["sharpe_2y"]))
    values.extend(_finite(row.get("value")) for row in rows or []
                  if row.get("name") == "LOW_2Y_SHARPE")
    if not values or any(value is None for value in values):
        return None
    return values[0] if all(value == values[0] for value in values) else None


def evaluate_candidate(detail: dict, check_payload: dict, region: str, cfg: dict,
                       correlations=None, pairwise=None,
                       expected_pairwise_ids=None, operator_count=None, local_self=None,
                       ppa=False, field_count=None, ppa_expression=None) -> dict:
    """Conservative settled Regular gate. Missing evidence never passes."""
    is_data = detail.get("is") if isinstance(detail, dict) else None
    is_data = is_data if isinstance(is_data, dict) else {}
    rows = _checks(detail, check_payload)
    gate = cfg["gate"]
    failures, pending = [], []
    ppa_eligibility = (evaluate_ppa(operator_count, field_count, is_data.get("sharpe"))
                       if ppa else None)
    if ppa_eligibility is not None:
        regular = detail.get("regular") if isinstance(detail, dict) else None
        if (not isinstance(ppa_expression, str) or not ppa_expression
                or not isinstance(regular, dict) or regular.get("code") != ppa_expression):
            ppa_eligibility["pending"].append("expression_unverified")
            if ppa_eligibility["status"] == "PASS":
                ppa_eligibility["status"] = "PENDING"
        failures.extend("ppa_" + item for item in ppa_eligibility["failures"])
        pending.extend("ppa_" + item for item in ppa_eligibility["pending"])
    if local_self is not None:
        local_state = local_self_correlation_state(local_self, gate.get("self_corr_max"))
        if local_state == "fail":
            failures.append("local_self_correlation")
        elif local_state != "pass":
            pending.append("local_self_correlation_unavailable")
    sharpe = _finite(is_data.get("sharpe"))
    region = (region or "").upper()
    if region not in SUPPORTED_REGIONS:
        pending.append("region_threshold_unavailable")
    elif sharpe is None:
        pending.append("sharpe_unavailable")
    elif region == "CHN":
        if sharpe < gate["china_sharpe_min"]:
            failures.append("sharpe")
    elif sharpe <= gate["sharpe_min"]:
        failures.append("sharpe")

    for label, value, limit in (
        ("fitness", _finite(is_data.get("fitness")), gate["fitness_min"]),
        ("two_year_sharpe", _two_year(is_data, rows), gate["two_year_sharpe_min"]),
        ("margin", _finite(is_data.get("margin")), gate["margin_min"]),
    ):
        if value is None:
            pending.append(label + "_unavailable")
        elif value <= limit:
            failures.append(label)
    turnover = _finite(is_data.get("turnover"))
    if turnover is None:
        pending.append("turnover_unavailable")
    elif not gate["turnover_min"] <= turnover <= gate["turnover_max"]:
        failures.append("turnover")
    if "riskNeutralized" in is_data:
        risk = is_data["riskNeutralized"]
        if not isinstance(risk, dict):
            pending.append("risk_neutralized_unavailable")
        else:
            defaults = DEFAULT_CONFIG["gate"]
            limits = (("sharpe", gate.get("risk_neutralized_sharpe_min",
                                           defaults["risk_neutralized_sharpe_min"])),
                      ("fitness", gate.get("risk_neutralized_fitness_min",
                                            defaults["risk_neutralized_fitness_min"])),
                      ("margin", gate["margin_min"]))
            for key, limit in limits:
                value = _finite(risk.get(key))
                limit = _finite(limit)
                if limit is None:
                    pending.append("risk_neutralized_" + key + "_threshold_unavailable")
                elif value is None:
                    pending.append("risk_neutralized_" + key + "_unavailable")
                elif value <= limit:
                    failures.append("risk_neutralized_" + key)
    ra_failures = []
    ra_pending = []
    if rows is None:
        pending.append("ra_checks_unavailable")
    else:
        for row in rows:
            result = str(row.get("result") or "").upper()
            if result in {"FAIL", "WARNING"}:
                ra_failures.append(row["name"])
            elif result == "PENDING":
                ra_pending.append(row["name"])
            elif result != "PASS":
                pending.append("ra_status_unreadable:" + row["name"])
    failures.extend("ra:" + name for name in ra_failures)
    pending.extend("ra_pending:" + name for name in ra_pending)
    correlation_allowed = (rows is not None and not ra_failures
                           and not any(item.startswith("ra_status_unreadable")
                                       for item in pending))
    if not correlation_allowed:
        status = ("BELOW_THRESHOLD" if "sharpe" in failures else
                  "RA_FAILED" if ra_failures else "EVIDENCE_PENDING")
        return {"status": status, "correlation_allowed": False,
                "ra_fail_count": len(ra_failures), "failures": failures,
                "pending": pending,
                **({"ppa_eligibility": ppa_eligibility} if ppa else {})}

    correlations = correlations or {}
    prod = _finite(correlations.get("prod"))
    self_corr = _finite(correlations.get("self"))
    expected = expected_pairwise_ids or []
    pairwise = pairwise or {}
    for name, value in (("prod", prod), ("self", self_corr)):
        if value is None:
            pending.append(name + "_correlation_unavailable")
        elif abs(value) > gate[name + "_corr_max"]:
            failures.append(name + "_correlation")
    for alpha_id in expected:
        value = _finite(pairwise.get(alpha_id))
        if value is None:
            pending.append("pairwise_unavailable:" + alpha_id)
        elif abs(value) >= gate["pairwise_corr_max"]:
            failures.append("pairwise:" + alpha_id)
    if any(item.startswith("pairwise:") for item in failures):
        status = "PAIRWISE_CONFLICT"
    elif ra_pending:
        status = "CHECKS_PENDING"
    elif any(item.endswith("correlation") for item in failures):
        status = "CORR_REJECTED"
    elif failures:
        status = "METRICS_FAILED"
    elif pending:
        status = "EVIDENCE_PENDING"
    else:
        status = "CANDIDATE"
    return {"status": status, "correlation_allowed": True,
            "ra_fail_count": 0, "failures": failures, "pending": pending,
            **({"ppa_eligibility": ppa_eligibility} if ppa else {})}


def parse_correlations(payload: dict, expected_pairwise_ids=None) -> dict:
    """Read values from raw correlation evidence, not the API's pass flags."""
    result = {"prod": None, "self": None, "pairwise": {}}
    checks = payload.get("checks") if isinstance(payload, dict) else None
    if not isinstance(checks, dict):
        return result
    case_evidence = payload.get("case_pairwise")
    if isinstance(case_evidence, dict):
        for alpha_id in expected_pairwise_ids or []:
            evidence = case_evidence.get(alpha_id)
            value = _finite(evidence.get("correlation")) if isinstance(evidence, dict) else None
            if (isinstance(evidence, dict) and evidence.get("source") == "daily_pnl"
                    and type(evidence.get("observations")) is int
                    and evidence["observations"] >= 2
                    and value is not None and -1 <= value <= 1):
                result["pairwise"][alpha_id] = abs(value)
    prod = checks.get("production") or {}
    prod_data = prod.get("correlation_data") if isinstance(prod, dict) else None
    if isinstance(prod_data, dict):
        value = _finite(prod_data.get("max"))
        if value is not None and -1 <= value <= 1:
            result["prod"] = abs(value)
    if result["prod"] is None and isinstance(prod, dict):
        check = terminal_correlation_check([prod.get("platform_check")], "PROD_CORRELATION")
        if check:
            result["prod"] = abs(_finite(check["value"]))

    self_check = checks.get("self") or {}
    if isinstance(self_check, dict):
        check = terminal_correlation_check([self_check.get("platform_check")], "SELF_CORRELATION")
        if check:
            result["self"] = abs(_finite(check["value"]))
    self_data = self_check.get("correlation_data") if isinstance(self_check, dict) else None
    if not isinstance(self_data, dict):
        return result
    records = self_data.get("records")
    if (not isinstance(records, list) or not records or self_data.get("next")
            or self_data.get("count", len(records)) != len(records)):
        return result
    schema = self_data.get("schema") or {}
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if (not isinstance(properties, list) or any(not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str) or not entry["name"]
            for entry in properties)):
        return result
    columns = [entry["name"] for entry in properties]
    if columns.count("correlation") != 1:
        return result
    corr_index = columns.index("correlation")
    id_index = next((columns.index(name) for name in ("alphaId", "alpha", "id")
                     if name in columns), None)
    values = []
    pairs = {}
    for record in records:
        if not isinstance(record, (list, tuple)) or len(record) <= corr_index:
            return result
        value = _finite(record[corr_index])
        if value is None or not -1 <= value <= 1:
            return result
        values.append(abs(value))
        if id_index is not None and len(record) > id_index:
            pairs[str(record[id_index])] = abs(value)
    result["self"] = max(values)
    for alpha_id in expected_pairwise_ids or []:
        if alpha_id in pairs:
            result["pairwise"][alpha_id] = max(result["pairwise"].get(alpha_id, 0),
                                               pairs[alpha_id])
    return result
