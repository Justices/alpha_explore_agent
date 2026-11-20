"""Validate live platform test-period evidence without guessing calendar days."""
import re

from alpha_research.workflows.regular.errors import ResearchError


def _duration_parts(value):
    """Parse year/month/day ISO durations returned by the platform schema."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?", value)
    if not match or not any(part is not None for part in match.groups()):
        return None
    try:
        years, months, days = (int(part or 0) for part in match.groups())
    except ValueError:
        return None
    return years * 12 + months, days


def _duration_difference_bounds(left, right):
    """Bound calendar-month differences without guessing their exact day counts."""
    months, days = left[0] - right[0], left[1] - right[1]
    return (months * (28 if months >= 0 else 31) + days,
            months * (31 if months >= 0 else 28) + days)


def _validate_test_period(value, rule):
    """Accept only periods demonstrably within live choices or duration bounds."""
    if not isinstance(rule, dict) or not isinstance(value, str) or not value:
        raise ResearchError("test_period is not verified by the live settings schema")
    choices = rule.get("choices")
    if isinstance(choices, list):
        if (not choices or any(not isinstance(item, dict) or
                               not isinstance(item.get("value"), str) for item in choices)
                or value not in [item["value"] for item in choices]):
            raise ResearchError("test_period is not supported by the live settings schema")
        return {"source": "platform_settings_schema", "value": value,
                "choices": choices}
    if rule.get("type") != "string":
        raise ResearchError("test_period is not verified by the live settings schema")
    minimum, maximum = rule.get("minValue"), rule.get("maxValue")
    candidate, lower, upper = map(_duration_parts, (value, minimum, maximum))
    if (candidate is None or lower is None or upper is None
            or _duration_difference_bounds(candidate, lower)[0] < 0
            or _duration_difference_bounds(candidate, upper)[1] > 0):
        raise ResearchError("test_period is not demonstrably within the live settings range")
    return {"source": "platform_settings_schema", "value": value,
            "minValue": minimum, "maxValue": maximum}
