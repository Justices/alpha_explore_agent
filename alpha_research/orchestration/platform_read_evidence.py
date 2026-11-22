"""P4.9 optional local audit record of a REAL read-only BRAIN probe.

The probe is performed separately through Gateway(read_only=True).
This module receives only the safe aggregate summary after success.
A local integrity checksum is NOT a platform signature or operator consent.
No API calls, credentials, models, Simulation or Alpha submission occur here.
"""
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from alpha_research.state.fingerprint import fingerprint_json
from alpha_research.state.sealed import is_sealed_state, write_sealed_json
from alpha_research.state.writer_lock import SingleWriterLock, WriterLockError

_FIELDS = frozenset({
    "status", "region", "delay", "universe",
    "platform_settings_readable", "dataset_catalog_readable",
    "dataset_count", "brain_read_invoked", "simulation_invoked",
    "model_invoked", "alpha_submission_invoked",
})
_HEX32 = re.compile(r"[0-9a-f]{32}\Z")


def _directory(state_root):
    return Path(state_root) / "state" / "research_v1_1" / "platform_read_evidence"


def _check_probe(probe):
    if (not isinstance(probe, dict) or set(probe) != _FIELDS
            or probe["status"] != "READ_ONLY_SCOPE_VERIFIED"
            or not isinstance(probe["region"], str)
            or not re.fullmatch(r"[A-Z]{3,8}", probe["region"])
            or type(probe["delay"]) is not int
            or probe["delay"] not in (0, 1)
            or not isinstance(probe["universe"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", probe["universe"])
            or type(probe["dataset_count"]) is not int
            or probe["dataset_count"] < 0
            or probe["platform_settings_readable"] is not True
            or probe["dataset_catalog_readable"] is not True
            or probe["brain_read_invoked"] is not True
            or probe["simulation_invoked"] is not False
            or probe["model_invoked"] is not False
            or probe["alpha_submission_invoked"] is not False):
        raise ValueError("platform read evidence requires successful read-only summary")


def record_platform_read_evidence(state_root, probe):
    """Append-only local record; never promotes a read into executable authority."""
    _check_probe(probe)
    root = _directory(state_root)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise ValueError("platform read evidence directory unsafe")
    stop = Path(state_root) / "state" / "stop.flag"
    if stop.exists() or stop.is_symlink():
        raise ValueError("platform read evidence cannot be recorded while stopped")
    from alpha_research.orchestration.mock_workspace import marker_path
    marker = marker_path(state_root)
    if marker.exists() or marker.is_symlink():
        raise ValueError("platform read evidence forbidden in mock workspace")
    record_id = uuid4().hex
    target = root / (record_id + ".json")
    record = {
        "schema_version": 1,
        "record_id": record_id,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source": "LOCAL_READONLY_BRAIN_PROBE",
        "source_sha256": fingerprint_json(probe),
        "report": dict(probe),
        "status": "LOCAL_READ_OBSERVED_NOT_ATTESTED",
        "platform_account_verified": False,
        "remote_operation_grant_verified": False,
        "permission_to_dispatch": False,
        "automatic_retry_allowed": False,
    }
    try:
        with SingleWriterLock(root / "evidence.lock"):
            if target.exists() or target.is_symlink() or root.is_symlink():
                raise ValueError("duplicate or unsafe platform read evidence ID")
            if stop.exists() or stop.is_symlink():
                raise ValueError("stop signal blocks evidence persistence")
            write_sealed_json(target, record)
    except WriterLockError as exc:
        raise ValueError("platform read evidence writer is busy") from exc
    return inspect_platform_read_evidence(state_root, record_id)


def inspect_platform_read_evidence(state_root, record_id):
    """Read-only checksum and provenance inspection; NO production approval."""
    if not isinstance(record_id, str) or _HEX32.fullmatch(record_id) is None:
        raise ValueError("invalid platform read evidence record ID")
    root = _directory(state_root)
    path = root / (record_id + ".json")
    try:
        if root.is_symlink() or path.is_symlink() or not path.is_file():
            raise ValueError("platform read evidence missing/unsafe")
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not is_sealed_state(data)
                or set(data) != {
                    "schema_version", "record_id", "recorded_at",
                    "source", "source_sha256", "report", "status",
                    "platform_account_verified", "remote_operation_grant_verified",
                    "permission_to_dispatch", "automatic_retry_allowed",
                    "state_sha256",
                }
                or type(data["schema_version"]) is not int
                or data["schema_version"] != 1
                or data["record_id"] != record_id
                or data["source"] != "LOCAL_READONLY_BRAIN_PROBE"
                or data["status"] != "LOCAL_READ_OBSERVED_NOT_ATTESTED"
                or data["platform_account_verified"] is not False
                or data["remote_operation_grant_verified"] is not False
                or data["permission_to_dispatch"] is not False
                or data["automatic_retry_allowed"] is not False
                or fingerprint_json(data["report"]) != data["source_sha256"]):
            raise ValueError("platform read evidence corrupted or mismatched")
        _check_probe(data["report"])
        report = data["report"]
        return {
            "schema_version": 1,
            "record_id": record_id,
            "recorded_at": data["recorded_at"],
            "status": "LOCAL_READ_OBSERVED_NOT_ATTESTED",
            "scope": {name: report[name] for name in ("region", "delay", "universe")},
            "dataset_count": report["dataset_count"],
            "local_integrity_verified": True,
            "platform_account_verified": False,
            "production_authorized": False,
            "permission_to_dispatch": False,
            "model_call_authorized": False,
            "alpha_submission_authorized": False,
        }
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return {
            "schema_version": 1, "record_id": record_id,
            "status": "RECONCILIATION_REQUIRED",
            "local_integrity_verified": False,
            "production_authorized": False,
            "permission_to_dispatch": False,
            "model_call_authorized": False,
            "alpha_submission_authorized": False,
        }
