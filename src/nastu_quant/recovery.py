"""Retrieval/reconciliation protocol (FR-08, AGENT_DESIGN.md §6.3).

When a multisim times out or returns fewer children than submitted,
NEVER resubmit first: reconcile against the platform by matching the
account's recent alphas to the batch's pre-registered expressions.
The reconciliation key is the hash of (expression, settings), which is
unique within a batch because the pre-registration list is unique.
"""
import hashlib
import json


SETTING_KEYS = {
    "instrument_type": "instrumentType",
    "unit_handling": "unitHandling",
    "nan_handling": "nanHandling",
    "test_period": "testPeriod",
    "max_trade": "maxTrade",
}


def reconciliation_key(expression: str, settings: dict) -> str:
    payload = json.dumps(
        {"expr": expression.strip(), "settings": settings},
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def extract_expression(alpha: dict):
    """Best-effort expression text from a get_user_alphas record."""
    reg = alpha.get("regular")
    if isinstance(reg, str):
        return reg
    if isinstance(reg, dict):
        return reg.get("code") or reg.get("expression")
    return alpha.get("expression") or alpha.get("code")


def _match(prereg_expressions, settings: dict, user_alphas):
    """Core matcher. Settings comparison follows OD-K1 (decided
    2026-10-27): a key ABSENT from the platform record does not veto
    the record (the absence is noted instead); a key that is present
    with a different value is a hard veto."""
    by_key = {}
    notes = {}
    for alpha in user_alphas or []:
        expr = extract_expression(alpha)
        observed_settings = alpha.get("settings")
        if not expr or not isinstance(observed_settings, dict):
            continue
        missing_keys = []
        veto = False
        for key, value in settings.items():
            mapped = SETTING_KEYS.get(key, key)
            if mapped not in observed_settings:
                missing_keys.append(mapped)
            elif observed_settings[mapped] != value:
                veto = True
                break
        if veto:
            continue
        key = reconciliation_key(expr, settings)
        by_key.setdefault(key, []).append(alpha)
        if missing_keys:
            notes.setdefault(expr, missing_keys)
    matched, missing = {}, []
    for expr in prereg_expressions:
        hits = by_key.get(reconciliation_key(expr, settings), [])
        if len(hits) == 1:
            matched[expr] = hits[0]
        else:
            missing.append(expr)
    return matched, missing, notes


def match_alphas(prereg_expressions, settings: dict, user_alphas):
    """Match platform alphas to pre-registered expressions.

    Returns (matched, missing): matched maps expression -> alpha record,
    missing lists expressions with no platform counterpart (only those
    may enter the single-simulation fallback queue)."""
    matched, missing, _notes = _match(prereg_expressions, settings, user_alphas)
    return matched, missing


def match_alphas_detailed(prereg_expressions, settings: dict, user_alphas):
    """Like match_alphas, plus a notes dict: expression -> settings keys
    absent from the matched platform record (OD-K1 audit trail)."""
    return _match(prereg_expressions, settings, user_alphas)
