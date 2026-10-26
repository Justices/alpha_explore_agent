"""Retrieval/reconciliation protocol (FR-08, AGENT_DESIGN.md §6.3).

When a multisim times out or returns fewer children than submitted,
NEVER resubmit first: reconcile against the platform by matching the
account's recent alphas to the batch's pre-registered expressions.
The reconciliation key is the hash of (expression, settings), which is
unique within a batch because the pre-registration list is unique.
"""
import hashlib
import json


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


def match_alphas(prereg_expressions, settings: dict, user_alphas):
    """Match platform alphas to pre-registered expressions.

    Returns (matched, missing): matched maps expression -> alpha record,
    missing lists expressions with no platform counterpart (only those
    may enter the single-simulation fallback queue)."""
    by_key = {}
    for alpha in user_alphas or []:
        expr = extract_expression(alpha)
        if expr:
            by_key[reconciliation_key(expr, settings)] = alpha
    matched, missing = {}, []
    for expr in prereg_expressions:
        hit = by_key.get(reconciliation_key(expr, settings))
        if hit is not None:
            matched[expr] = hit
        else:
            missing.append(expr)
    return matched, missing
