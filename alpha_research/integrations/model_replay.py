"""Provider-free batch replay with fail-closed audit verification.

This module has no model or SDK imports. It cannot retry remote requests.
Any crash gap, mismatched route, corrupt manifest or unknown outcome blocks.
"""
import hashlib
import json
from pathlib import Path

from alpha_research.integrations.model_errors import ModelError
from alpha_research.state.fingerprint import canonical_json_bytes as _json_bytes
from alpha_research.state.store import atomic_write_json


def replay_batch_call(batch_dir, task, context_sha, route_sha=None, *,
                      append_event, on_verified=None, expected_research_id=None):
    if batch_dir is None:
        return None
    manifest_path = batch_dir / "model_call_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
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
        if row.get("task") != task or row.get("context_sha256") != context_sha:
            continue
        if row.get("route_sha256") != route_sha:
            raise ModelError(
                "recorded model role/backend differs from current configuration; "
                "an explicit research migration is required")
        if (not isinstance(row.get("response_ref"), str)
                or not row["response_ref"]
                or not isinstance(row.get("global_record_ref"), str)
                or not row["global_record_ref"]):
            raise ModelError("existing batch model manifest has invalid audit paths")
        response_path = Path(row["response_ref"])
        if not response_path.exists() and row.get("global_response_ref"):
            response_path = Path(row["global_response_ref"])
        try:
            payload = json.loads(response_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError) as exc:
            raise ModelError(
                "previous model response is missing or unreadable; retry blocked"
            ) from exc
        record_path = Path(row.get("global_record_ref") or "")
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = None
        if not isinstance(record, dict):
            raise ModelError("recorded model decision is missing its audit record")
        if (record.get("call_id") != row["call_id"]
                or (expected_research_id is not None
                    and record.get("research_id") != expected_research_id)
                or record.get("task") != task
                or record.get("context_sha256") != context_sha
                or record.get("route_sha256") != route_sha):
            raise ModelError("recorded model audit identity mismatch; replay blocked")
        if not isinstance(payload, dict):
            raise ModelError("previous model response is malformed; retry blocked")
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
        if payload.get("status") in {"ok", "error"} and record.get("status") == "in_flight":
            record["status"] = payload["status"]
            record["response_sha256"] = hashlib.sha256(_json_bytes(payload)).hexdigest()
            record["token_in"] = payload.get("token_in")
            record["token_out"] = payload.get("token_out")
            atomic_write_json(record_path, record)
            append_event(record)
        # Reconcile only after response/manifest/audit identity and hashes
        # have been verified. Ledger settlement is idempotent under its lock.
        if on_verified is not None:
            on_verified(row["call_id"], payload)
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

