"""Trusted local evidence paths for provider-free model-call recovery.

The batch manifest is untrusted input: it may describe a call, but it must
never choose which file the recovery process reads or rewrites. The expected
locations are derived exclusively from trusted roots and the call ID.
"""
import re
from pathlib import Path

from alpha_research.integrations.model_errors import ModelError


_CALL_ID = re.compile(r"[0-9a-f]{32}\Z")


def check_trusted_path(path, *, label):
    """Reject symlinks in the final file or any parent, even when absent."""
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        raise ModelError(f"model {label} escapes trusted evidence path")
    return path


def check_manifest_reference(reference, expected, *, label):
    """Permit precisely the canonical generated filename, not a caller URL."""
    expected = check_trusted_path(expected, label=label)
    if not isinstance(reference, str) or reference != str(expected):
        raise ModelError(f"model {label} is not the trusted evidence path")
    return expected


def trusted_replay_paths(batch_dir, calls_root, row):
    """Return canonical local artifacts after validating *every* manifest ref."""
    if not isinstance(row, dict):
        raise ModelError("existing batch model manifest has invalid call entries")
    call_id = row.get("call_id")
    if not isinstance(call_id, str) or _CALL_ID.fullmatch(call_id) is None:
        raise ModelError("existing batch model manifest has invalid call entries")
    batch = check_trusted_path(batch_dir, label="batch directory")
    calls = check_trusted_path(calls_root, label="global calls directory")
    copied = batch / "model_calls"
    if not isinstance(row.get("session_id"), str) or _CALL_ID.fullmatch(row["session_id"]) is None:
        raise ModelError("existing batch model manifest has invalid session identity")
    paths = {
        "context_ref": copied / f"{call_id}.context.json",
        "record_ref": copied / f"{call_id}.record.json",
        "response_ref": copied / f"{call_id}.response.json",
        "global_record_ref": calls / f"{call_id}.record.json",
        "global_response_ref": calls / f"{call_id}.response.json",
    }
    for key, expected in paths.items():
        check_manifest_reference(row.get(key), expected, label=key)
    # The global context path is authoritative but intentionally not stored
    # in the batch manifest; it is reconstructed from the call ID.
    paths["global_context_ref"] = check_trusted_path(
        calls / f"{call_id}.context.json", label="global_context_ref")
    return paths
