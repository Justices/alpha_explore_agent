"""Cache keys for completed BRAIN Regular requests and account history."""

import hashlib
import json


_SETTINGS = {
    "instrumentType", "region", "universe", "delay", "decay", "neutralization",
    "truncation", "pasteurization", "unitHandling", "nanHandling", "language",
    "visualization", "testPeriod", "maxTrade",
}


def _normalize(value):
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def cache_record(payload):
    settings = _normalize(payload["settings"])
    return {"expression": payload["regular"], "dataset_id": "PLATFORM_REGULAR",
            "scope": {key: settings[key] for key in ("region", "delay", "universe")},
            "settings": settings}


def submission_key(payload):
    """Identify a platform request independently of its batch and research."""
    normalized = _normalize(payload)
    expressions = ("regular",) if payload["type"] == "REGULAR" else ("selection", "combo")
    for key in expressions:
        normalized[key] = normalized[key].strip()
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def history_payload(alpha):
    """Project only complete FASTEXPR history without supplying absent defaults."""
    if alpha.get("type") != "REGULAR":
        return None
    settings = alpha.get("settings")
    required = _SETTINGS
    if (not isinstance(settings, dict) or settings.get("language") != "FASTEXPR"
            or any(settings.get(key) is None for key in required)):
        return None
    payload = {"type": "REGULAR", "settings": {
        key: value for key, value in settings.items() if key not in {"startDate", "endDate"}}}
    if payload["settings"].get("simulationMode") == "FULL":
        del payload["settings"]["simulationMode"]
    if set(payload["settings"]) != required:
        return None
    expression = alpha.get("regular")
    if isinstance(expression, dict):
        expression = expression.get("code")
    if not isinstance(expression, str) or not expression.strip():
        return None
    payload["regular"] = expression
    return payload
