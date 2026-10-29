"""Reconcile live audit evidence using this project's configured gates."""

import math

from .gatekeeper import RA_CHECK_NAMES, evaluate_candidate, parse_correlations
from .recovery import SETTING_KEYS
from .ppa import ppa_gate_kwargs


_EXTERNAL_METRIC_FAILURES = {
    "primary_sharpe", "fitness", "2y_sharpe", "margin", "turnover",
    "risk_neutralized_sharpe", "risk_neutralized_fitness",
    "risk_neutralized_margin", "individual_correlation_exceeded",
}


def _correlation(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return abs(value) if math.isfinite(value) and -1 <= value <= 1 else None


def _string_list(value):
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def reconcile_audit_evidence(alpha_id, result, local_gate, readback, prereg, config,
                             expected_expression):
    """Return local failures and unresolved evidence without importing foreign limits."""
    pending, failures = [], []
    findings = result.get("gate_failures")
    unresolved = result.get("gate_pending")
    metric_findings = result.get("candidate_metric_failures")
    evidence_flag = (result.get("evidence_complete") if result.get("audit_kind") == "local_evidence"
                     else result.get("candidate_eligible"))
    if (not _string_list(findings) or not _string_list(unresolved)
            or not _string_list(metric_findings)
            or not isinstance(evidence_flag, bool)):
        return {"failures": [], "pending": ["canonical_audit:malformed_result"]}
    if (evidence_flag != (not findings and not unresolved)
            or not set(metric_findings).issubset(set(findings) | set(unresolved))):
        pending.append("canonical_audit:inconsistent_result")

    details = result.get("raw_details")
    is_data = details.get("is") if isinstance(details, dict) else None
    if (not isinstance(details, dict) or details.get("id") != alpha_id
            or not isinstance(is_data, dict)):
        return {"failures": [], "pending": sorted(set(
            pending + ["canonical_audit:readback_unavailable"]))}
    if details.get("type") != "REGULAR":
        pending.append("canonical_audit:regular_alpha_type_unverified")
    checks = is_data.get("checks")
    if not isinstance(checks, list):
        pending.append("canonical_audit:platform_checks_unavailable")
    else:
        for check in checks:
            if not isinstance(check, dict) or not isinstance(check.get("name"), str) or not check["name"]:
                pending.append("canonical_audit:platform_check_unreadable")
                continue
            name = check["name"]
            if name in RA_CHECK_NAMES:
                continue
            state = str(check.get("result") or "").upper()
            if state == "FAIL":
                failures.append("platform_check:" + name)
            elif state in {"PENDING", "WARNING"}:
                pending.append("platform_check_pending:" + name)
            elif state != "PASS":
                pending.append("platform_check_unreadable:" + name)
    scope = prereg.get("scope") if isinstance(prereg.get("scope"), dict) else {}
    expected_settings = prereg.get("simulation_settings")
    observed_settings = details.get("settings")
    if not isinstance(expected_settings, dict) or not expected_settings or not isinstance(observed_settings, dict):
        pending.append("canonical_audit:settings_unverified")
    else:
        for key, value in expected_settings.items():
            names = {key, SETTING_KEYS.get(key, key)}
            observed = [observed_settings[name] for name in names if name in observed_settings]
            if (not observed or any(item != value or
                                    (isinstance(value, bool) and not isinstance(item, bool))
                                    for item in observed)):
                pending.append("canonical_audit:settings_unverified")
                break

    admission = result.get("field_admission")
    admission_scope = admission.get("scope") if isinstance(admission, dict) else None
    fields = admission.get("fields") if isinstance(admission, dict) else None
    expected_scope = {key: scope.get(key) for key in ("region", "universe", "delay")}
    expected_scope["instrument_type"] = (expected_settings.get("instrument_type")
                                          if isinstance(expected_settings, dict) else None)
    if (not isinstance(admission, dict) or admission.get("passed") is not True
            or admission.get("dataset") != prereg.get("dataset_id")
            or not isinstance(admission_scope, dict)
            or any(value is None or admission_scope.get(key) != value
                   for key, value in expected_scope.items())
            or not isinstance(fields, list) or not fields
            or any(not isinstance(field, dict) or not isinstance(field.get("id"), str)
                   or not field["id"] for field in fields)
            or len({field["id"] for field in fields}) != len(fields)
            or admission.get("failures") != []):
        pending.append("canonical_audit:field_admission_unverified")

    previous = readback.get("details") if isinstance(readback, dict) else None
    if not isinstance(previous, dict) or not isinstance(previous.get("is"), dict):
        pending.append("canonical_audit:prior_readback_unavailable")
    elif previous["is"] != is_data:
        pending.append("canonical_audit:readback_changed")

    local_corr = local_gate.get("correlations")
    if not isinstance(local_corr, dict):
        local_corr = {}
        pending.append("canonical_audit:local_correlation_unavailable")
    local_pairs = local_corr.get("pairwise")
    if not isinstance(local_pairs, dict):
        local_pairs = {}
        pending.append("canonical_audit:local_pairwise_unavailable")
    pairs = {}
    for other, value in local_pairs.items():
        value = _correlation(value)
        if not isinstance(other, str) or not other or value is None:
            pending.append("canonical_audit:local_pairwise_unavailable")
        else:
            pairs[other] = value

    combined = {}
    for key in ("prod", "self"):
        local_value = _correlation(local_corr.get(key))
        raw_value = result.get(key + "_corr")
        audit_value = _correlation(raw_value)
        if raw_value is not None and audit_value is None:
            pending.append("canonical_audit:invalid_" + key + "_correlation")
        if raw_value is None and "individual_correlation_unavailable" not in unresolved:
            pending.append("canonical_audit:" + key + "_correlation_unavailable")
        values = [value for value in (local_value, audit_value) if value is not None]
        combined[key] = max(values) if values else None

    raw_correlations = readback.get("correlation_raw") if isinstance(readback, dict) else None
    parsed = parse_correlations(raw_correlations, list(pairs))
    local_verified = (all(_correlation(local_corr.get(key)) is not None
                          and _correlation(parsed.get(key)) == _correlation(local_corr.get(key))
                          for key in ("prod", "self"))
                      and parsed.get("pairwise") == pairs)

    audit_pairs = result.get("pairwise")
    pair_state = result.get("pairwise_state")
    conflicts = 0
    if (not isinstance(audit_pairs, list) or pair_state not in {"evaluated", "not_applicable"}
            or (pair_state == "not_applicable" and audit_pairs)
            or (pair_state == "evaluated" and not audit_pairs)):
        pending.append("canonical_audit:pairwise_evidence_unavailable")
        audit_pairs = []
    for row in audit_pairs:
        pair = row.get("pair") if isinstance(row, dict) else None
        value = _correlation(row.get("corr")) if isinstance(row, dict) else None
        state = row.get("verdict") if isinstance(row, dict) else None
        if (not isinstance(pair, list) or len(pair) != 2 or alpha_id not in pair
                or any(not isinstance(item, str) or not item for item in pair)
                or pair[0] == pair[1] or state not in {"pass", "conflict", "pending", "measured"}
                or value is None or state == "pending"):
            pending.append("canonical_audit:pairwise_evidence_unavailable")
            continue
        other = pair[1] if pair[0] == alpha_id else pair[0]
        pairs[other] = max(pairs.get(other, 0.0), value)
        if state == "conflict":
            conflicts += 1

    for item in unresolved:
        if item == "pairwise_conflict_requires_user_choice" and conflicts:
            continue
        if item == "individual_correlation_unavailable" and local_verified:
            continue
        pending.append("canonical_audit:" + item)

    regular = details.get("regular")
    expression = regular.get("code") if isinstance(regular, dict) else None
    if expression != expected_expression or result.get("expression") != expected_expression:
        pending.append("canonical_audit:expression_mismatch")
    operator_count = (prereg.get("operator_counts") or {}).get(expected_expression)
    ppa_args = ppa_gate_kwargs(prereg, expected_expression)
    if ppa_args["ppa"]:
        validation = (prereg.get("validation") or {}).get(expected_expression)
        validated_fields = validation.get("fields") if isinstance(validation, dict) else None
        if (not isinstance(fields, list) or not isinstance(validated_fields, list)
                or {item.get("id") for item in fields if isinstance(item, dict)} != set(validated_fields)):
            ppa_args["field_count"] = None
        if not isinstance(validation, dict) or validation.get("operator_count") != operator_count:
            operator_count = None

    latest = evaluate_candidate(
        details, {"checks": is_data.get("checks")},
        (prereg.get("scope") or {}).get("region"), config,
        correlations=combined, pairwise=pairs, expected_pairwise_ids=list(pairs),
        operator_count=operator_count, **ppa_args,
        local_self=(result.get("local_self_correlation") or {}
                    if result.get("audit_kind") == "local_evidence" else None))
    failures.extend(latest.get("failures", []))
    pending.extend(latest.get("pending", []))

    for item in findings:
        if item in _EXTERNAL_METRIC_FAILURES or item.startswith("unsupported_region_threshold:"):
            continue
        if item == "ra_failed_count" and latest.get("ra_fail_count"):
            continue
        pending.append("canonical_audit:unmapped_failure:" + item)

    return {"failures": sorted(set(failures)), "pending": sorted(set(pending)),
            "local_gate": latest, "correlations": {**combined, "pairwise": pairs}}
