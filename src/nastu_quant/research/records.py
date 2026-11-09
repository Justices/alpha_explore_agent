"""Versioned V1.1 research records, separate from legacy CampaignStore.

Callers must hold the existing single-writer lock for read-modify-write
operations. This module does not schedule work or post simulations.
"""

import copy
import json
import re
from datetime import datetime
from pathlib import Path

from ..state_store import atomic_write_json


SCHEMA_VERSIONS = {
    "campaign": 2,
    "dataset_subtask": 1,
    "combination": 1,
    "optimization_source": 1,
    "candidate_branch": 1,
    "super_run": 1,
    "candidate": 1,
    "preferred": 1,
}
ID_PATTERN = re.compile(r"[0-9a-f]{16,64}\Z")
IMMUTABLE_FIELDS = {
    "campaign": ("campaign_id", "workflow", "request", "created_at"),
    "dataset_subtask": ("subtask_id", "campaign_id", "dataset_id", "created_at"),
    "combination": ("combination_id", "subtask_id", "effective_settings",
                    "support_evidence_ref", "created_at"),
    "optimization_source": ("source_id", "alpha_id", "source_type",
                            "snapshot_ref", "created_at"),
    "candidate_branch": ("branch_id", "source_id", "source_alpha_id",
                         "source_snapshot_ref", "source_snapshot_sha256",
                         "source_properties_sha256", "source_type", "created_at"),
    "super_run": ("run_id", "workflow", "region", "created_at"),
    "candidate": ("candidate_id", "alpha_id", "workflow", "source_ref", "candidate_ref", "created_at"),
    "preferred": ("candidate_id", "alpha_id", "workflow", "source_ref", "candidate_ref", "created_at"),
}
RECORD_IDS = {
    "campaign": "campaign_id",
    "dataset_subtask": "subtask_id",
    "combination": "combination_id",
    "optimization_source": "source_id",
    "candidate_branch": "branch_id",
    "super_run": "run_id",
    "candidate": "candidate_id",
    "preferred": "candidate_id",
}


def _nonempty(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")


def _id(value, field):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be a 16-64 character lowercase hex ID")


def _list(value, field):
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")


def _timestamp(value, field):
    _nonempty(value, field)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must have a timezone")


def _validate_budget(value):
    if not isinstance(value, dict):
        raise ValueError("budget must be an object")
    for key in ("limits", "reserved", "used"):
        if not isinstance(value.get(key), dict):
            raise ValueError(f"budget.{key} must be an object")
    limits = value["limits"]
    if not limits:
        raise ValueError("budget requires at least one limit")
    if set(value["reserved"]) != set(limits) or set(value["used"]) != set(limits):
        raise ValueError("budget counters must match limits")
    for key, limit in limits.items():
        if not isinstance(key, str) or not key:
            raise ValueError("budget dimension must be named")
        reserved, used = value["reserved"][key], value["used"][key]
        if any(isinstance(n, bool) or not isinstance(n, int) for n in
               (limit, reserved, used)):
            raise ValueError(f"budget.{key} values must be integers")
        if limit < 1 or reserved < 0 or used < 0 or reserved + used > limit:
            raise ValueError(f"budget.{key} exceeds its limit")


def validate_record(kind: str, record: dict) -> None:
    """Reject malformed records before they can be persisted."""
    if kind not in SCHEMA_VERSIONS:
        raise ValueError(f"unknown research record kind: {kind}")
    if not isinstance(record, dict):
        raise ValueError("research record must be an object")
    version = record.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSIONS[kind]:
        raise ValueError(f"{kind} requires schema version {SCHEMA_VERSIONS[kind]}")
    _id(record.get(RECORD_IDS[kind]), RECORD_IDS[kind])
    for field in ("created_at", "updated_at"):
        _timestamp(record.get(field), field)
    _nonempty(record.get("status"), "status")

    if kind == "campaign":
        if record.get("workflow") != "regular":
            raise ValueError("campaign workflow must be regular")
        request = record.get("request")
        if not isinstance(request, dict):
            raise ValueError("campaign request must be an object")
        for field in ("objective", "region"):
            _nonempty(request.get(field), f"request.{field}")
        datasets = request.get("dataset_ids")
        if datasets is not None:
            _list(datasets, "request.dataset_ids")
            for item in datasets:
                _nonempty(item, "request.dataset_ids item")
            if not datasets or len(datasets) != len(set(datasets)):
                raise ValueError("request.dataset_ids must be unique and nonempty")
        if request.get("profile") not in ("regular", "ppa"):
            raise ValueError("request.profile must be regular or ppa")
        for field in ("category", "delay", "universe"):
            if field not in request:
                raise ValueError(f"request.{field} must be present, possibly null")
        revision = record.get("scope_revision")
        if type(revision) is not int or revision < 1:
            raise ValueError("scope_revision must be positive")
        _validate_budget(record.get("budget"))
        _list(record.get("dataset_subtask_ids"), "dataset_subtask_ids")
        for item in record["dataset_subtask_ids"]:
            _id(item, "dataset_subtask_id")
    elif kind == "dataset_subtask":
        _id(record.get("campaign_id"), "campaign_id")
        _nonempty(record.get("dataset_id"), "dataset_id")
        _list(record.get("combination_ids"), "combination_ids")
        for item in record["combination_ids"]:
            _id(item, "combination_id")
    elif kind == "combination":
        _id(record.get("subtask_id"), "subtask_id")
        settings = record.get("effective_settings")
        if not isinstance(settings, dict):
            raise ValueError("effective_settings must be an object")
        for field in ("region", "dataset_id", "delay", "universe"):
            if settings.get(field) is None:
                raise ValueError(f"effective_settings.{field} is required")
        _nonempty(record.get("support_evidence_ref"), "support_evidence_ref")
    elif kind == "optimization_source":
        _nonempty(record.get("alpha_id"), "alpha_id")
        if record.get("source_type") not in ("regular", "super"):
            raise ValueError("source_type must be regular or super")
        _nonempty(record.get("region"), "region")
        _nonempty(record.get("snapshot_ref"), "snapshot_ref")
    elif kind == "candidate_branch":
        _id(record.get("source_id"), "source_id")
        _nonempty(record.get("source_alpha_id"), "source_alpha_id")
        _nonempty(record.get("source_snapshot_ref"), "source_snapshot_ref")
        if record.get("source_type") not in ("regular", "super"):
            raise ValueError("source_type must be regular or super")
        _nonempty(record.get("rationale"), "rationale")
        _list(record.get("changes"), "changes")
        if not isinstance(record.get("effective_settings"), dict):
            raise ValueError("effective_settings must be an object")
    elif kind in ("candidate", "preferred"):
        _nonempty(record.get("alpha_id"), "alpha_id")
        if record.get("workflow") not in ("regular", "super", "optimization"):
            raise ValueError("workflow must be regular, super or optimization")
        _nonempty(record.get("source_ref"), "source_ref")
        _nonempty(record.get("candidate_ref"), "candidate_ref")
        _list(record.get("failures"), "failures")
        _list(record.get("pending"), "pending")
        if not isinstance(record.get("metrics"), dict) or not isinstance(record.get("evidence_refs"), dict):
            raise ValueError("metrics and evidence_refs must be objects")
        if kind == "preferred" and record.get("status") not in ("PREFERRED", "NOT_PREFERRED", "PENDING_PREFERRED", "NOT_CANDIDATE"):
            raise ValueError("invalid Preferred status")
    elif kind == "super_run":
        if record.get("workflow") != "super":
            raise ValueError("super run workflow must be super")
        _nonempty(record.get("region"), "region")
        _nonempty(record.get("objective"), "objective")
        _validate_budget(record.get("budget"))


class ResearchRecordStore:
    """Store V1.1 records without reading or modifying legacy campaign files."""

    def __init__(self, state_root: Path):
        self.root = Path(state_root).resolve() / "state" / "research_v1_1"

    def path_for(self, kind: str, record_id: str) -> Path:
        if kind not in SCHEMA_VERSIONS:
            raise ValueError(f"unknown research record kind: {kind}")
        _id(record_id, "record_id")
        return self.root / kind / f"{record_id}.json"

    def get(self, kind: str, record_id: str) -> dict:
        path = self.path_for(kind, record_id)
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, ValueError) as exc:
            raise ValueError(f"research record is unreadable: {path}") from exc
        validate_record(kind, record)
        if record[RECORD_IDS[kind]] != record_id:
            raise ValueError(f"research record ID disagrees with path: {path}")
        return record

    def list(self, kind: str) -> list[dict]:
        """Return validated records of one kind in stable ID order."""
        if kind not in SCHEMA_VERSIONS:
            raise ValueError(f"unknown research record kind: {kind}")
        folder = self.root / kind
        if not folder.exists():
            return []
        return [self.get(kind, path.stem) for path in sorted(folder.glob("*.json"))]

    def put(self, kind: str, record: dict, *, expected_updated_at=None) -> Path:
        """Create, or replace with an explicit last-read timestamp.

        The caller holds SingleWriterLock. Existing identity and source
        lineage cannot be changed by a replacement.
        """
        validate_record(kind, record)
        path = self.path_for(kind, record[RECORD_IDS[kind]])
        try:
            previous = self.get(kind, record[RECORD_IDS[kind]])
        except FileNotFoundError:
            if expected_updated_at is not None:
                raise ValueError("record disappeared before update")
        else:
            if expected_updated_at != previous["updated_at"]:
                raise ValueError("record changed since it was read")
            for field in IMMUTABLE_FIELDS[kind]:
                if record.get(field) != previous.get(field):
                    raise ValueError(f"{field} cannot change in place")
            if datetime.fromisoformat(record["updated_at"]) <= datetime.fromisoformat(previous["updated_at"]):
                raise ValueError("updated_at must advance")
        atomic_write_json(path, copy.deepcopy(record))
        return path
