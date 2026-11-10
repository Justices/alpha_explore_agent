"""Calculate case correlations from complete, aligned cumulative PnL evidence."""
from datetime import datetime
import math


def parse_pnl_recordset(payload):
    """Return dated cumulative PnL, or None when the recordset is incomplete."""
    if not isinstance(payload, dict) or payload.get("next"):
        return None
    schema = payload.get("schema")
    properties = schema.get("properties") if isinstance(schema, dict) else None
    records = payload.get("records")
    if not isinstance(properties, list) or not isinstance(records, list):
        return None
    names = [item.get("name") if isinstance(item, dict) else None for item in properties]
    if names.count("date") != 1 or names.count("pnl") != 1:
        return None
    count = payload.get("count")
    if count is not None and (type(count) is not int or count != len(records)):
        return None
    date_index, pnl_index = names.index("date"), names.index("pnl")
    result = {}
    for row in records:
        if not isinstance(row, (list, tuple)) or max(date_index, pnl_index) >= len(row):
            return None
        raw_date, raw_pnl = row[date_index], row[pnl_index]
        if not isinstance(raw_date, str) or isinstance(raw_pnl, bool):
            return None
        try:
            day = datetime.fromisoformat(raw_date.strip().replace("Z", "+00:00")).date()
            value = float(raw_pnl)
        except (TypeError, ValueError, OverflowError):
            return None
        if day in result or not math.isfinite(value):
            return None
        result[day] = value
    return result


def pairwise_pnl_correlation(left_payload, right_payload, *, window_years=None):
    """Correlate daily changes over matching date intervals, with provenance."""
    left, right = parse_pnl_recordset(left_payload), parse_pnl_recordset(right_payload)
    if not left or not right:
        return None

    def changes(series):
        days = sorted(series)
        return {days[i]: (days[i - 1], series[days[i]] - series[days[i - 1]])
                for i in range(1, len(days))}

    left_changes, right_changes = changes(left), changes(right)
    common = sorted(day for day in left_changes.keys() & right_changes.keys()
                    if left_changes[day][0] == right_changes[day][0])
    if window_years is not None:
        if type(window_years) is not int or window_years <= 0:
            return None
        latest = max(left)
        try:
            cutoff = latest.replace(year=latest.year - window_years)
        except ValueError:
            try:
                cutoff = latest.replace(year=latest.year - window_years, day=28)
            except ValueError:
                return None
        common = [day for day in common if day > cutoff and left_changes[day][0] >= cutoff]
    if len(common) < 2:
        return None
    x = [left_changes[day][1] for day in common]
    y = [right_changes[day][1] for day in common]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    vx, vy = sum((value - mx) ** 2 for value in x), sum((value - my) ** 2 for value in y)
    if vx <= 0 or vy <= 0 or not math.isfinite(vx) or not math.isfinite(vy):
        return None
    correlation = (sum((a - mx) * (b - my) for a, b in zip(x, y)) /
                   (math.sqrt(vx) * math.sqrt(vy)))
    if not math.isfinite(correlation) or abs(correlation) > 1.0 + 1e-12:
        return None
    return {"source": "daily_pnl", "correlation": max(-1.0, min(1.0, correlation)),
            "observations": len(common), "start_date": common[0].isoformat(),
            "end_date": common[-1].isoformat()}
