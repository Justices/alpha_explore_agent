"""Build local provenance and diagnostic tag proposals."""

from alpha_research.qualification.gates import local_self_correlation_state


def research_tags(request):
    tags = [f"DS:{request['dataset_id']}"]
    origin = request.get("origin_alpha_id")
    if origin:
        return tags + ["OPT_ALPHA", f"ORG:{origin}"]
    return tags + ["ORG_ALPHA"]


def diagnostic_properties(record):
    if not record:
        return {}
    evidence = record.get("evidence_refs") or {}
    local = evidence.get("local_self_correlation")
    if isinstance(local, dict) and local_self_correlation_state(local, local.get("limit")) == "fail":
        kind, value = "sc", local["value"]
    else:
        kind, value = "pc", (record.get("correlations") or {}).get("prod")
    try:
        value = abs(float(value)) if not isinstance(value, bool) else None
    except (TypeError, ValueError, OverflowError):
        return {}
    if value is None or not 0 <= value <= 1:
        return {}
    color = next(color for threshold, color in (
        (0.75, "RED"), (0.70, "YELLOW"), (0.66, "GREEN"), (0.50, "BLUE"), (-1, "PURPLE"))
        if value > threshold)
    return {"name": f"{kind}_{round(value, 4)}", "color": color}
