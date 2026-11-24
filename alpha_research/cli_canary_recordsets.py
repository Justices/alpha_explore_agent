"""Explicit, minimal BRAIN GET-only recordset observation for one pinned Canary.

No simulation, correlation, tag, submission or model operation is reachable.
Authentication is handled by WorldQuantClient and may POST /authentication;
all subsequent platform requests in this module are exact allowlisted GETs.
This evidence describes Alpha-level recordsets, NOT raw analyst DataField
availability or per-instrument portfolio weights.
"""
import hashlib
import json
import re
from pathlib import Path

from alpha_research.cli_canary import CanaryError, EXPRESSION, SCOPE, _canary_path, _read
from alpha_research.cli_canary_field_decision import decide_fields_canary
from alpha_research.integrations.brain.runtime import WorldQuantClient
from alpha_research.state.store import atomic_write_json, file_sha256

RECORDSETS = ("coverage", "coverage-by-sector", "turnover",
              "average-value-by-sector")
MAX_BYTES = 8 * 1024 * 1024
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\\Z")


def _fingerprint(value):
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True,
                         allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise CanaryError("recordset JSON exceeds offline evidence size limit")
    return hashlib.sha256(encoded).hexdigest()


def _get(client, path):
    # Literal allowlist prevents arbitrary network requests and new endpoints.
    if (not isinstance(path, str)
            or re.fullmatch(r"/alphas/[A-Za-z0-9_-]{1,128}(?:/recordsets(?:/(?:coverage|coverage-by-sector|turnover|average-value-by-sector))?)?", path) is None):
        raise CanaryError("BRAIN read path is outside Canary GET allowlist")
    response = client.request("GET", path)
    status = getattr(response, "status_code", None)
    if status != 200:
        return {"status": "UNAVAILABLE", "http_status": (
            status if type(status) is int and 100 <= status <= 599 else None)}
    raw = getattr(response, "content", b"")
    if isinstance(raw, bytes) and len(raw) > MAX_BYTES:
        return {"status": "UNAVAILABLE", "reason": "PAYLOAD_TOO_LARGE"}
    try:
        data = response.json()
        _fingerprint(data)
    except (ValueError, TypeError, OverflowError):
        return {"status": "UNAVAILABLE", "reason": "INVALID_JSON"}
    except CanaryError:
        return {"status": "UNAVAILABLE", "reason": "PAYLOAD_TOO_LARGE"}
    if not isinstance(data, dict):
        return {"status": "UNAVAILABLE", "reason": "UNEXPECTED_SHAPE"}
    return {"status": "AVAILABLE", "data": data}


def _verify_alpha(details, alpha_id):
    if details.get("id") != alpha_id or details.get("type") != "REGULAR":
        raise CanaryError("BRAIN Alpha identity or type does not match pinned Canary")
    reg = details.get("regular")
    code = reg.get("code") if isinstance(reg, dict) else reg
    if code != EXPRESSION:
        raise CanaryError("BRAIN Alpha expression differs from pinned Canary")
    settings = details.get("settings")
    if not isinstance(settings, dict):
        raise CanaryError("BRAIN Alpha settings are absent")
    for actual_key, scope_key in (
            ("instrumentType", "instrument_type"), ("region", "region"),
            ("delay", "delay"), ("universe", "universe")):
        expected = SCOPE[scope_key]
        actual = settings.get(actual_key)
        if type(actual) is not type(expected) or actual != expected:
            raise CanaryError("BRAIN Alpha scope differs from pinned Canary")


def _listed(data):
    rows = data.get("results")
    count = data.get("count")
    if (type(count) is not int or count < 0
            or not isinstance(rows, list) or len(rows) != count
            or count > 100):
        raise CanaryError("BRAIN recordset index is invalid or incomplete")
    names = []
    for item in rows:
        name = item.get("name") if isinstance(item, dict) else None
        if (not isinstance(name, str)
                or re.fullmatch(r"[a-z0-9-]{1,64}", name) is None
                or name in names):
            raise CanaryError("BRAIN recordset index has invalid entries")
        names.append(name)
    return names


def observe_canary_recordsets(root, plan_id, *, client=None, snapshot_root=None):
    """Read exact one-Alpha official recordsets with immutable provenance."""
    root = Path(root).resolve()
    decision = decide_fields_canary(root, plan_id, snapshot_root=snapshot_root)
    if (decision.get("simulation_authorized") is not False
            or decision.get("candidate_eligible") is not False):
        raise CanaryError("Canary must remain in the offline evidence gate")
    alpha_id = decision.get("source_alpha_id")
    if not isinstance(alpha_id, str) or _ID.fullmatch(alpha_id) is None:
        raise CanaryError("invalid pinned Alpha ID")
    decision_path = _canary_path(root, plan_id).with_name(plan_id + "_round3_decision.json")
    original_decision = _read(decision_path)
    if original_decision != {k: v for k, v in decision.items()
                            if k != "decision_path"}:
        raise CanaryError("immutable field decision differs")

    owns_client = client is None
    if client is None:
        client = WorldQuantClient()
    try:
        alpha_read = _get(client, "/alphas/" + alpha_id)
        if alpha_read["status"] != "AVAILABLE":
            return {
                "schema_version": 1, "status": "BRAIN_ALPHA_READ_UNAVAILABLE",
                "plan_id": plan_id, "source_alpha_id": alpha_id,
                "read_status": alpha_read, "source_provenance_verified": False,
                "candidate_eligible": False, "simulation_authorized": False,
                "model_invoked": False, "simulation_invoked": False,
                "alpha_submission_invoked": False, "alpha_tags_modified": False,
                "next_stage": "VERIFY_ALPHA_READ_ACCESS_AND_PINNED_IDENTITY",
            }
        _verify_alpha(alpha_read["data"], alpha_id)
        detail_sha = _fingerprint(alpha_read["data"])
        listed = _get(client, "/alphas/" + alpha_id + "/recordsets")
        if listed["status"] != "AVAILABLE":
            names = []
            catalog_state = listed
        else:
            names = _listed(listed["data"])
            catalog_state = {"status": "AVAILABLE",
                             "sha256": _fingerprint(listed["data"])}
        payloads = {}
        observations = {}
        for name in RECORDSETS:
            if name not in names:
                observations[name] = {"status": "NOT_LISTED"}
                continue
            result = _get(client, "/alphas/" + alpha_id + "/recordsets/" + name)
            if result["status"] != "AVAILABLE":
                observations[name] = result
                continue
            obj = result["data"]
            # Preserve exact JSON for later schema-aware offline analysis.
            # No date series, percentage, or holdings are inferred here.
            sha = _fingerprint(obj)
            payloads[name] = obj
            observations[name] = {
                "status": "CAPTURED_SCHEMA_REVIEW_REQUIRED",
                "sha256": sha,
                "schema_name": (obj.get("schema", {}).get("name")
                                if isinstance(obj.get("schema"), dict) else None),
                "record_count": (len(obj["records"])
                                 if isinstance(obj.get("records"), list) else None),
            }
        report = {
            "schema_version": 1,
            "status": ("ALPHA_RECORDSETS_CAPTURED_SCHEMA_REVIEW_REQUIRED"
                       if payloads else "ALPHA_RECORDSETS_UNAVAILABLE"),
            "plan_id": plan_id, "source_alpha_id": alpha_id,
            "scope": dict(SCOPE), "expression": EXPRESSION,
            "source_field_decision_sha256": file_sha256(decision_path),
            "brain_alpha_detail_sha256": detail_sha,
            "recordset_catalog": catalog_state,
            "recordsets": observations,
            "alpha_level_recordset_not_raw_field_or_position_evidence": True,
            "alpha_identity_settings_readback_verified": True,
            "source_provenance_verified": bool(payloads),
            "recordset_provenance_verified": bool(payloads),
            "candidate_eligible": False, "simulation_authorized": False,
            "model_invoked": False, "simulation_invoked": False,
            "alpha_submission_invoked": False, "alpha_tags_modified": False,
            "next_stage": ("OFFLINE_RECORDSET_SCHEMA_AND_METRIC_REVIEW"
                           if payloads else
                           "VERIFY_RECORDSET_ACCESS_OR_OTHER_EVIDENCE"),
        }
        envelope = {"report": report, "payloads": payloads}
        digest = _fingerprint(envelope)[:16]
        path = _canary_path(root, plan_id).with_name(
            plan_id + "_recordsets_" + digest + ".json")
        if path.exists() or path.is_symlink():
            if _read(path) != envelope:
                raise CanaryError("recordset evidence exists with differing bytes")
        else:
            atomic_write_json(path, envelope)
        return {**report, "recordset_evidence_path": str(path)}
    finally:
        if owns_client:
            client.close()
