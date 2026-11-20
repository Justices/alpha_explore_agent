"""Validate and merge durable model-session indices across research batches.

A session may be bound to several batches. The historical `batch_ref`
field remains the initial binding; `call_ids` is the unique union.
The additive `batches` field records each batch's exact call membership.
"""
import re
from pathlib import Path

from alpha_research.integrations.model_errors import ModelError


_SESSION_ID = re.compile(r"[0-9a-f]{32}\Z")


def index_path(logs_root, session_id):
    if not isinstance(session_id, str) or _SESSION_ID.fullmatch(session_id) is None:
        raise ModelError("model session ID must be a 32-character lowercase hex ID")
    return Path(logs_root) / "model_sessions" / (session_id + ".json")


def _ids(items):
    if (not isinstance(items, list)
            or any(not isinstance(item, str) or not item for item in items)
            or len(set(items)) != len(items)):
        raise ModelError("model session index has invalid call IDs")
    return sorted(items)


def merge_index(existing, session_id, batch_ref, call_ids, *,
                research_id=None):
    """Return one deterministic index; never discard prior batch relationships.

    Legacy three-field records are upgraded in place only when a batch is
    rebound. Broken new-schema histories fail closed rather than being reset.
    """
    incoming = _ids(call_ids)
    if research_id is not None and (
            not isinstance(research_id, str) or not research_id.strip()):
        raise ModelError("model session research ID is invalid")
    if existing is None:
        primary = batch_ref
        entries = {}
    else:
        if (not isinstance(existing, dict)
                or existing.get("session_id") != session_id
                or not isinstance(existing.get("batch_ref"), str)
                or not existing["batch_ref"]):
            raise ModelError("model session index identity is invalid")
        prior_research = existing.get("research_id")
        if (prior_research is not None and
                (not isinstance(prior_research, str) or not prior_research.strip())):
            raise ModelError("model session index research identity is invalid")
        if (research_id is not None and prior_research is not None
                and research_id != prior_research):
            raise ModelError("model session ID belongs to a different research")
        if research_id is None:
            research_id = prior_research
        primary = existing["batch_ref"]
        legacy_ids = _ids(existing.get("call_ids"))
        existing_batches = existing.get("batches")
        if existing_batches is None:
            entries = {primary: legacy_ids}
        else:
            if not isinstance(existing_batches, list):
                raise ModelError("model session index has invalid batches")
            entries = {}
            for row in existing_batches:
                if (not isinstance(row, dict)
                        or not isinstance(row.get("batch_ref"), str)
                        or not row["batch_ref"]
                        or row["batch_ref"] in entries):
                    raise ModelError("model session index has invalid batch references")
                entries[row["batch_ref"]] = _ids(row.get("call_ids"))
            if (primary not in entries
                    or legacy_ids != sorted({c for ids in entries.values() for c in ids})):
                raise ModelError("model session index conflicts with batch membership")
    entries[batch_ref] = incoming
    aggregate = sorted({call for ids in entries.values() for call in ids})
    result = {
        "session_id": session_id,
        "batch_ref": primary,
        "call_ids": aggregate,
        "batches": [{"batch_ref": ref, "call_ids": entries[ref]}
                    for ref in sorted(entries)],
    }
    if research_id is not None:
        result["research_id"] = research_id
    return result
