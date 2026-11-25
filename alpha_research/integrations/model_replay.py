"""Provider-free batch replay with fail-closed audit verification.

This module has no model or SDK imports. It cannot retry remote requests.
Any crash gap, mismatched route, corrupt manifest or unknown outcome blocks.
"""
import hashlib

from alpha_research.integrations.model_errors import ModelError
from alpha_research.integrations.model_evidence import (
    check_trusted_path, read_trusted_json, trusted_replay_paths,
)
from alpha_research.state.fingerprint import canonical_json_bytes as _json_bytes
from alpha_research.state.store import atomic_write_json
from alpha_research.state.sealed import is_sealed_state


def replay_batch_call(batch_dir, task, context_sha, route_sha=None, *,
                      calls_root, append_event, on_verified=None,
                      expected_research_id, expected_call_id=None):
    if batch_dir is None:
        return None
    if not isinstance(expected_research_id, str) or not expected_research_id.strip():
        raise ModelError("model replay requires a trusted research identity")
    manifest_path = check_trusted_path(
        batch_dir / "model_call_manifest.json", label="batch manifest")
    try:
        manifest = read_trusted_json(manifest_path, label="batch manifest")
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ModelError("existing batch model manifest is unreadable; replay blocked") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("calls"), list):
        raise ModelError("existing batch model manifest schema is invalid")
    rows = manifest["calls"]
    if (any(not isinstance(row, dict)
            or not isinstance(row.get("call_id"), str)
            or not row["call_id"] for row in rows)
            or len({row["call_id"] for row in rows}) != len(rows)):
        raise ModelError("existing batch model manifest has invalid call entries")
    for row in rows:
        # Check every stored call against its immutable origin before deciding
        # whether it matches this task. An attacker must not be able to change
        # context_sha256 or task in a row to silently trigger a second call.
        paths = trusted_replay_paths(batch_dir, calls_root, row)
        record_path = paths["global_record_ref"]
        try:
            record = read_trusted_json(record_path, label="global audit")
        except (OSError, ValueError) as exc:
            raise ModelError("recorded model decision is missing its audit record") from exc
        if not isinstance(record, dict):
            raise ModelError("recorded model decision is missing its audit record")
        if (record.get("call_id") != row["call_id"]
                or record.get("session_id") != row["session_id"]
                or record.get("task") != row.get("task")
                or record.get("context_sha256") != row.get("context_sha256")
                or record.get("route_sha256") != row.get("route_sha256")
                or record.get("batch_ref") not in (
                    str(batch_dir), f"session:{row['session_id']}")
                or record.get("context_ref") != str(paths["global_context_ref"])
                or record.get("response_ref") != str(paths["global_response_ref"])):
            raise ModelError("recorded model audit identity mismatch; replay blocked")
        try:
            stored_context = read_trusted_json(paths["global_context_ref"], label="global context")
            input_sha = hashlib.sha256(_json_bytes(stored_context)).hexdigest()
        except (OSError, ValueError, TypeError) as exc:
            raise ModelError("recorded model input is unreadable; replay blocked") from exc
        if input_sha != row["context_sha256"]:
            raise ModelError("recorded model input checksum mismatch; replay blocked")
        if (row.get("task") != task or row.get("context_sha256") != context_sha
                or (expected_call_id is not None and row["call_id"] != expected_call_id)):
            continue
        if record.get("research_id") != expected_research_id:
            raise ModelError("recorded model audit identity mismatch; replay blocked")
        if row.get("route_sha256") != route_sha:
            raise ModelError(
                "recorded model role/backend differs from current configuration; "
                "replay blocked")
        response_path = (paths["global_response_ref"] if expected_call_id is not None
                         else paths["response_ref"])
        if not response_path.exists():
            response_path = paths["global_response_ref"]
        try:
            payload = read_trusted_json(response_path, label="model response")
            if expected_call_id is not None and paths["response_ref"].exists():
                copied = read_trusted_json(paths["response_ref"], label="batch response")
                if _json_bytes(copied) != _json_bytes(payload):
                    raise ModelError("batch response differs from original response")
        except (OSError, ValueError, KeyError) as exc:
            raise ModelError(
                "previous model response is missing or unreadable; retry blocked"
            ) from exc
        if not is_sealed_state(payload):
            raise ModelError(
                "previous model response integrity seal checksum mismatch or missing; "
                "budget reconciliation and retry blocked")
        response_sha = hashlib.sha256(_json_bytes(payload)).hexdigest()
        if row.get("response_sha256") is not None and row["response_sha256"] != response_sha:
            raise ModelError("recorded model manifest checksum mismatch")
        if payload.get("status") not in {"ok", "error"}:
            raise ModelError("previous model response has invalid status; retry blocked")
        if payload["status"] == "ok" and not isinstance(payload.get("result"), dict):
            raise ModelError("previous model decision must be a JSON object")
        if (payload["status"] == "error"
                and (not isinstance(payload.get("error_type"), str)
                     or not isinstance(payload.get("error"), str))):
            raise ModelError("previous model error evidence is incomplete; retry blocked")
        if (record.get("status") == "in_flight"
                and (record.get("token_in") is not None
                     or record.get("token_out") is not None)):
            raise ModelError("in-flight model audit has invalid usage; replay blocked")
        if record.get("status") != "in_flight" and (
                record.get("token_in") != payload.get("token_in")
                or record.get("token_out") != payload.get("token_out")):
            raise ModelError("recorded model usage mismatch; replay blocked")
        if record.get("status") not in {"in_flight", payload.get("status")}:
            raise ModelError("recorded model status mismatch; replay blocked")
        if record.get("status") != "in_flight" and not record.get("response_sha256"):
            raise ModelError("recorded model terminal checksum missing; replay blocked")
        if record.get("response_sha256"):
            if record["response_sha256"] != response_sha:
                raise ModelError("recorded model response checksum mismatch")
        if expected_call_id is not None and (
                record.get("status") != "ok" or payload["status"] != "ok"):
            raise ModelError("explicit reconciliation requires a terminal successful audit")
        # Settle the verified original reservation before repairing the audit
        # status. If a conflicting or UNKNOWN ledger rejects reconciliation,
        # no new terminal audit event is written. Both steps are independently
        # idempotent after a crash in either order.
        if on_verified is not None:
            on_verified(row["call_id"], payload)
        if record.get("status") == "in_flight":
            record["status"] = payload["status"]
            record["response_sha256"] = response_sha
            record["token_in"] = payload.get("token_in")
            record["token_out"] = payload.get("token_out")
            atomic_write_json(record_path, record)
            append_event(record)
        if payload["status"] == "ok":
            return payload["result"]
        # Offline mock decisions have no network/API side effects. An
        # explicitly logged, terminal model error can be retried during
        # fixture recovery. A real provider's failure is not proof that
        # the request was not processed and must stay blocked.
        if (payload.get("status") == "error"
                and record.get("status") == "error"
                and record.get("provider") in {None, "mock"}):
            continue
        raise ModelError(
            "previous model decision is not a verified successful result; "
            "an explicit state reconciliation is required")
    return None

