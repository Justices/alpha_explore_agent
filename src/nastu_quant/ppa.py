"""User-requested Power Pool Alpha research rules."""

import math
import re

PPA_MAX_OPERATORS = 8
PPA_MAX_FIELDS = 3
PPA_MIN_SHARPE = 1.0

_PPA = re.compile(
    r"(?<![A-Za-z0-9_])(?:ppa(?:\s*[(（]\s*power[\s-]+pool[\s-]+alpha\s*[)）])?"
    r"|power[\s-]+pool[\s-]+alpha(?:\s*[(（]\s*ppa\s*[)）])?)(?![A-Za-z0-9_])", re.I)
_NEGATION = re.compile(
    r"(?:不(?:要|用|需(?:要)?|应)?|无需|排除|非)[^，。！？;；.\n]{0,8}$"
    r"|(?:\b(?:not|without|exclude|non|no)\b(?:[\s-]+\w+){0,3})[\s-]*$", re.I)
_REFERENCE = re.compile(r"(?:参考|参照|借鉴|对照|比较|对比|reference|compare|benchmark)\s*$", re.I)
_NOT_TARGET = re.compile(r"^\s*(?:不是|并非|不(?:是|做|研究|探索|需要)|不作为(?:研究)?目标|is\s+not)", re.I)
_REFERENCE_AFTER = re.compile(
    r"^\s*(?:(?:为|作为|用作|仅(?:作为|供|作)?|只是)(?:参考|对照|对比)"
    r"|as\s+(?:a\s+)?(?:reference|benchmark))", re.I)
_DIRECT_TARGET = re.compile(
    r"(?:探索|研究|构建|生成|挖掘|寻找|开发|explore|research|build|develop|create)\s*$", re.I)


def ppa_requested(objective):
    """Recognize an explicit PPA target; negated mentions do not select it."""
    if not isinstance(objective, str):
        return False
    matches = list(_PPA.finditer(objective))
    for match in matches:
        prefix = objective[max(0, match.start() - 40):match.start()]
        suffix = objective[match.end():match.end() + 40]
        if (_NEGATION.search(prefix) or _REFERENCE.search(prefix) or _NOT_TARGET.search(suffix)
                or (_REFERENCE_AFTER.search(suffix) and not _DIRECT_TARGET.search(prefix))):
            continue
        return True
    return False


def evaluate_ppa(operator_count, field_count, sharpe):
    """Classify the three supplied PPA rules from local, readable evidence."""
    failures, pending = [], []
    for name, value, limit in (("operator_count", operator_count, PPA_MAX_OPERATORS),
                               ("field_count", field_count, PPA_MAX_FIELDS)):
        if type(value) is not int or value < 0:
            pending.append(name + "_unavailable")
        elif value > limit:
            failures.append(name)
    try:
        number = float(sharpe) if not isinstance(sharpe, bool) else None
    except (TypeError, ValueError, OverflowError):
        number = None
    if number is None or not math.isfinite(number):
        pending.append("sharpe_unavailable")
    elif number < PPA_MIN_SHARPE:
        failures.append("sharpe")
    return {"status": "FAIL" if failures else "PENDING" if pending else "PASS",
            "operator_count": operator_count, "field_count": field_count,
            "sharpe": number if number is not None and math.isfinite(number) else None,
            "failures": failures, "pending": pending}


def ppa_gate_kwargs(prereg, expression):
    """Use locked per-expression validation to supply structural PPA evidence."""
    research_type = prereg.get("research_type")
    if research_type not in {None, "PPA", "REGULAR"}:
        raise ValueError("unrecognized research type")
    enabled = (research_type == "PPA" if research_type is not None
               else ppa_requested(prereg.get("objective")))
    validation = (prereg.get("validation") or {}).get(expression)
    fields = validation.get("fields") if isinstance(validation, dict) else None
    if (not isinstance(fields, list) or any(not isinstance(field, str) or not field for field in fields)
            or len(set(fields)) != len(fields)):
        fields = None
    return {"ppa": enabled, "ppa_expression": expression if enabled else None,
            "field_count": len(fields) if fields is not None else None}
