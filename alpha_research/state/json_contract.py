"""Strict local audit-record JSON loader.

A malformed, duplicate-key, non-finite, non-object or symlinked record can
never be promoted to an authoritative Task or recovery journal. Callers
choose their domain-specific error type; this helper performs no mutations.
"""
import json
from pathlib import Path


def _no_duplicate_fields(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate JSON field")
        output[key] = value
    return output


def _forbid_nonfinite(_value):
    raise ValueError("nonfinite JSON number")


def read_strict_object(path, label, *, error_type=ValueError):
    location = Path(path)
    if location.is_symlink() or location.parent.is_symlink():
        raise error_type(f"{label} has an untrusted symlink path")
    try:
        record = json.loads(
            location.read_text(encoding="utf-8"),
            object_pairs_hook=_no_duplicate_fields,
            parse_constant=_forbid_nonfinite,
        )
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise error_type(f"{label} is missing or unreadable") from exc
    if not isinstance(record, dict):
        raise error_type(f"{label} must contain a JSON object")
    return record
