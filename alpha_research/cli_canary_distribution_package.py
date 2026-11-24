"""Package local, real instrument-level CSV exports for an offline Canary audit.

No platform, model, network, or research-write capability is imported. Caller
attestations and source descriptions are retained as claims, never promoted
to independently verified platform provenance.
"""
from pathlib import Path

from alpha_research.cli_canary import CanaryError, FIELD, SCOPE, _canary_path, _read
from alpha_research.cli_canary_field_decision import decide_fields_canary
from alpha_research.cli_canary_distribution import (
    REQUIRED, _file, _measure, _read_csv, audit_distribution,
)
from alpha_research.state.store import atomic_write_json, file_sha256


def package_distribution(root, plan_id, *, evidence_dir=None,
                         universe_source, signal_source, weights_source,
                         universe_complete=False, signal_exhaustive=False,
                         weights_exhaustive=False, snapshot_root=None):
    """Bind source CSV bytes to an immutable manifest and run offline audit."""
    root = Path(root).resolve()
    decision = decide_fields_canary(root, plan_id, snapshot_root=snapshot_root)
    if (decision.get("simulation_authorized") is not False
            or decision.get("candidate_eligible") is not False):
        raise CanaryError("field decision does not preserve offline-only scope")

    directory = (Path(evidence_dir) if evidence_dir is not None else
                 root / "evidence" / "canary" / plan_id)
    if directory.is_symlink():
        raise CanaryError("evidence directory must not be a symlink")
    descriptions = {
        "universe.csv": universe_source,
        "signal.csv": signal_source,
        "weights.csv": weights_source,
    }
    if any(not isinstance(v, str) or not v.strip() or len(v) > 512
           for v in descriptions.values()):
        raise CanaryError("each export needs a nonempty source description")
    attestations = {
        "universe_complete": universe_complete,
        "signal_exhaustive": signal_exhaustive,
        "weights_exhaustive": weights_exhaustive,
    }
    if any(type(v) is not bool for v in attestations.values()):
        raise CanaryError("completeness must use explicit boolean claims")
    files = {name: directory / name for name in REQUIRED}
    missing = [name for name, path in files.items() if not path.is_file()]
    if missing:
        return {
            "schema_version": 1,
            "status": "DISTRIBUTION_SOURCE_CSV_MISSING",
            "plan_id": plan_id,
            "source_alpha_id": decision["source_alpha_id"],
            "missing_inputs": missing,
            "manifest_created": False,
            "computed_metrics": None,
            "source_provenance_verified": False,
            "simulation_authorized": False,
            "candidate_eligible": False,
            "model_invoked": False,
            "simulation_invoked": False,
            "alpha_submission_invoked": False,
            "alpha_tags_modified": False,
            "next_stage": "OBTAIN_OR_EXPORT_ORIGINAL_INSTRUMENT_LEVEL_CSVS",
        }
    for path in files.values():
        _file(path)
    first_hashes = {name: file_sha256(path) for name, path in files.items()}
    universe = _read_csv(files["universe.csv"], REQUIRED["universe.csv"])
    signals = _read_csv(files["signal.csv"], REQUIRED["signal.csv"])
    weights = _read_csv(files["weights.csv"], REQUIRED["weights.csv"])
    # Validate complete claims, memberships, date alignment, finite weights,
    # and gross exposure before publishing the manifest.
    _measure(universe, signals, weights, attestations)
    if any(file_sha256(path) != first_hashes[name]
           for name, path in files.items()):
        raise CanaryError("CSV evidence changed during packaging")

    manifest = {
        "schema_version": 1,
        "plan_id": plan_id,
        "alpha_id": decision["source_alpha_id"],
        "field_id": FIELD,
        "scope": dict(SCOPE),
        "source_type": "LOCAL_EXPORT",
        "completeness": attestations,
        "source_descriptions": descriptions,
        "files_sha256": first_hashes,
    }
    manifest_path = directory / "evidence_manifest.json"
    if manifest_path.exists() or manifest_path.is_symlink():
        if _read(manifest_path) != manifest:
            raise CanaryError("existing evidence manifest differs; never overwrite")
        created = False
    else:
        atomic_write_json(manifest_path, manifest)
        created = True
    # The audit rechecks manifest, CSV hashes and upstream immutable decision.
    report = audit_distribution(
        root, plan_id, evidence_dir=directory, snapshot_root=snapshot_root)
    return {**report, "manifest_created": created}
