"""Strict model-session index contract for a research spanning multiple batches.

Only the current v2 shape is supported. No inferred primary batch, legacy
three-field index, migration or reconstruction of unverifiable history.
"""
import re
from pathlib import Path

from alpha_research.integrations.model_errors import ModelError


SESSION_INDEX_VERSION = 2
_HEX_ID = re.compile(r"[0-9a-f]{32}\Z")


def index_path(logs_root, session_id):
    if not isinstance(session_id, str) or _HEX_ID.fullmatch(session_id) is None:
        raise ModelError("model session ID must be a 32-character lowercase hex ID")
    return Path(logs_root) / "model_sessions" / (session_id + ".json")


def _ids(items):
    if (not isinstance(items, list)
            or any(not isinstance(item, str)
                   or _HEX_ID.fullmatch(item) is None for item in items)
            or len(set(items)) != len(items)):
        raise ModelError("model session index has invalid call IDs")
    return sorted(items)


def merge_index(existing, session_id, research_id, batch_ref, call_ids):
    """Validate v2 identity and merge one batch without dropping other batches."""
    index_path(".", session_id)
    if not isinstance(research_id, str) or not research_id.strip():
        raise ModelError("model session research ID is invalid")
    if not isinstance(batch_ref, str) or not batch_ref:
        raise ModelError("model session batch reference is invalid")
    incoming = _ids(call_ids)
    entries = {}
    if existing is not None:
        if (not isinstance(existing, dict)
                or set(existing) != {"schema_version", "session_id",
                                     "research_id", "call_ids", "batches"}
                or existing["schema_version"] != SESSION_INDEX_VERSION
                or existing["session_id"] != session_id):
            raise ModelError("model session index does not match current contract")
        if existing["research_id"] != research_id:
            raise ModelError("model session ID belongs to a different research")
        rows = existing["batches"]
        if not isinstance(rows, list):
            raise ModelError("model session index has invalid batches")
        for row in rows:
            if (not isinstance(row, dict)
                    or set(row) != {"batch_ref", "call_ids"}
                    or not isinstance(row["batch_ref"], str)
                    or not row["batch_ref"]
                    or row["batch_ref"] in entries):
                raise ModelError("model session index has invalid batch references")
            entries[row["batch_ref"]] = _ids(row["call_ids"])
        aggregate = sorted({call for ids in entries.values() for call in ids})
        if _ids(existing["call_ids"]) != aggregate:
            raise ModelError("model session index conflicts with batch membership")

    # An existing call cannot be attributed to multiple batch directories.
    prior_members = {call: ref for ref, ids in entries.items() for call in ids
                     if ref != batch_ref}
    if any(call in prior_members for call in incoming):
        raise ModelError("model session call belongs to another batch")
    entries[batch_ref] = incoming
    aggregate = sorted({call for ids in entries.values() for call in ids})
    return {
        "schema_version": SESSION_INDEX_VERSION,
        "session_id": session_id,
        "research_id": research_id,
        "call_ids": aggregate,
        "batches": [{"batch_ref": ref, "call_ids": entries[ref]}
                    for ref in sorted(entries)],
    }
