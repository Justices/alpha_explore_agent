"""Offline evidence-bounded field coverage and portfolio concentration audit.

Reads *only* explicit, local exported data. The export manifest is a human
attestation, not BRAIN proof; therefore numerical results never establish
the actual platform CONCENTRATED_WEIGHT mechanism on their own.
No network/model/Simulation/Alpha mutation capabilities are imported.
"""
import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from alpha_research.cli_canary import CanaryError, FIELD, SCOPE, _canary_path, _read
from alpha_research.cli_canary_field_decision import decide_fields_canary
from alpha_research.state.store import atomic_write_json, file_sha256


REQUIRED = {
    "universe.csv": ["date", "instrument_id", "sector"],
    "signal.csv": ["date", "instrument_id", "field_value"],
    "weights.csv": ["date", "instrument_id", "portfolio_weight"],
}
MAX_INPUT_BYTES = 120 * 1024 * 1024
MAX_ROWS_PER_FILE = 2_000_000
MAX_DATES = 366


def _file(path, *, max_bytes=MAX_INPUT_BYTES):
    if path.is_symlink() or not path.is_file():
        raise CanaryError("local evidence is missing or is a symlink")
    size = path.stat().st_size
    if size == 0 or size > max_bytes:
        raise CanaryError("local evidence size is invalid")
    return path


def _number(text, *, nullable=False):
    if text == "" and nullable:
        return None
    if not isinstance(text, str) or not text.strip():
        raise CanaryError("evidence contains a missing numerical value")
    try:
        value = float(text)
    except ValueError as exc:
        raise CanaryError("evidence contains a nonnumeric value") from exc
    if not math.isfinite(value):
        raise CanaryError("evidence contains NaN or infinite value")
    return value


def _day(raw):
    if not isinstance(raw, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw) is None:
        raise CanaryError("invalid ISO date in evidence")
    try:
        date.fromisoformat(raw)
    except ValueError as exc:
        raise CanaryError("invalid calendar date in evidence") from exc
    return raw


def _identity(raw):
    if not isinstance(raw, str) or not raw or len(raw) > 128 or not raw.isprintable():
        raise CanaryError("invalid instrument identifier")
    return raw


def _read_csv(path, expected_columns):
    result = {}
    dates = set()
    _file(path)
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != expected_columns:
                raise CanaryError("evidence CSV columns differ from required schema")
            for count, row in enumerate(reader, start=1):
                if count > MAX_ROWS_PER_FILE:
                    raise CanaryError("evidence CSV exceeds row limit")
                if None in row or any(v is None for v in row.values()):
                    raise CanaryError("evidence CSV contains extra or malformed fields")
                day = _day(row["date"])
                instrument = _identity(row["instrument_id"])
                key = (day, instrument)
                if key in result:
                    raise CanaryError("duplicate instrument-date evidence row")
                if "sector" in row:
                    sector = row["sector"]
                    if not sector or len(sector) > 128 or not sector.isprintable():
                        raise CanaryError("invalid sector label")
                    value = sector
                elif "field_value" in row:
                    value = _number(row["field_value"], nullable=True)
                else:
                    value = _number(row["portfolio_weight"])
                result[key] = value
                dates.add(day)
                if len(dates) > MAX_DATES:
                    raise CanaryError("evidence spans too many dates")
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CanaryError("evidence CSV is unreadable") from exc
    if not result:
        raise CanaryError("evidence CSV has no data rows")
    return result


def _manifest(path, *, plan_id, alpha_id, files):
    _file(path, max_bytes=64 * 1024)
    manifest = _read(path)
    if (manifest.get("schema_version") != 1
            or manifest.get("plan_id") != plan_id
            or manifest.get("alpha_id") != alpha_id
            or manifest.get("field_id") != FIELD
            or manifest.get("scope") != SCOPE
            or manifest.get("source_type") != "LOCAL_EXPORT"):
        raise CanaryError("manifest identity or scope is inconsistent")
    completeness = manifest.get("completeness")
    provenance = manifest.get("source_descriptions")
    digests = manifest.get("files_sha256")
    if (not isinstance(completeness, dict)
            or set(completeness) != {
                "universe_complete", "signal_exhaustive", "weights_exhaustive"}
            or any(type(v) is not bool for v in completeness.values())
            or not isinstance(provenance, dict) or set(provenance) != set(REQUIRED)
            or not all(isinstance(v, str) and v.strip() and len(v) <= 512
                       for v in provenance.values())
            or not isinstance(digests, dict) or set(digests) != set(REQUIRED)):
        raise CanaryError("manifest completeness, source, or checksum evidence missing")
    for filename in REQUIRED:
        expected = digests[filename]
        if (not isinstance(expected, str) or
                re.fullmatch(r"[a-f0-9]{64}", expected) is None or
                file_sha256(files[filename]) != expected):
            raise CanaryError("evidence CSV hash differs from manifest")
    return completeness, digests


def _round(value):
    return round(value, 8)


def _measure(universe, signals, weights, completeness):
    universe_keys = set(universe)
    if not set(signals).issubset(universe_keys) or not set(weights).issubset(universe_keys):
        raise CanaryError("signal/weights contain an instrument outside the dated universe")
    dates = sorted({day for day, _ in universe})
    signals_dates = {day for day, _ in signals}
    weights_dates = {day for day, _ in weights}
    if signals_dates != set(dates) or weights_dates != set(dates):
        raise CanaryError("signal/weights dates do not match the provided universe")
    if completeness["signal_exhaustive"] and set(signals) != universe_keys:
        raise CanaryError("manifest claims exhaustive signal export but rows are missing")
    if not completeness["weights_exhaustive"]:
        # Normalizing only a portfolio subset gives misleading HHI/top-weight.
        weight_validity = "SAMPLE_ONLY_NO_PORTFOLIO_CONCENTRATION_INFERENCE"
    else:
        weight_validity = "EXHAUSTIVE_AS_ATTESTED_NOT_INDEPENDENTLY_VERIFIED"
    universe_by_date = defaultdict(list)
    for (day, instrument), sector in universe.items():
        universe_by_date[day].append((instrument, sector))
    per_date = []
    group_totals = defaultdict(lambda: [0, 0])
    normalized_weights = {}
    for day in dates:
        members = universe_by_date[day]
        nonnull = sum(signals.get((day, instrument)) is not None
                      and (day, instrument) in signals
                      for instrument, _ in members)
        for instrument, sector in members:
            group_totals[sector][0] += 1
            if (day, instrument) in signals and signals[(day, instrument)] is not None:
                group_totals[sector][1] += 1
        day_weights = {
            instrument: value for (d, instrument), value in weights.items()
            if d == day}
        gross = sum(abs(v) for v in day_weights.values())
        net = sum(day_weights.values())
        if gross <= 0:
            raise CanaryError("daily portfolio gross weight is zero")
        shares = sorted((abs(v) / gross for v in day_weights.values()), reverse=True)
        hhi = sum(x * x for x in shares)
        normalized_weights[day] = {
            "max_gross_weight_share": _round(shares[0]),
            "top5_gross_weight_share": _round(sum(shares[:5])),
            "gross_weight_hhi": _round(hhi),
            "effective_gross_holdings": _round(1 / hhi),
            "gross_exposure_input_units": _round(gross),
            "net_exposure_input_units": _round(net),
            "portfolio_constituents_in_export": len(day_weights),
        }
        per_date.append({
            "date": day,
            "universe_rows": len(members),
            "signal_rows": sum((day, instrument) in signals for instrument, _ in members),
            "nonnull_signal_rows": nonnull,
            "nonnull_fraction_of_exported_universe": _round(nonnull / len(members)),
            "portfolio": normalized_weights[day] if completeness["weights_exhaustive"] else None,
        })
    sector_coverage = [
        {"sector": sec, "universe_instrument_dates": n,
         "nonnull_instrument_dates": valid,
         "nonnull_fraction": _round(valid / n)}
        for sec, (n, valid) in sorted(group_totals.items())
    ]
    # Only adjacent exported days are compared. No inference of market-day
    # cadence and no assumption that missing values were unchanged.
    updates = []
    for prior, following in zip(dates, dates[1:]):
        overlapping = {inst for d, inst in universe if d == prior} & {
            inst for d, inst in universe if d == following}
        eligible = [inst for inst in overlapping
                    if (prior, inst) in signals and (following, inst) in signals
                    and signals[(prior, inst)] is not None
                    and signals[(following, inst)] is not None]
        changes = sum(signals[(prior, inst)] != signals[(following, inst)]
                      for inst in eligible)
        updates.append({
            "from_date": prior, "to_date": following,
            "nonnull_pairs": len(eligible),
            "changed_pairs": changes,
            "change_fraction_of_comparable_pairs": (
                _round(changes / len(eligible)) if eligible else None),
        })
    full_scope_claimed = (completeness["universe_complete"]
                          and completeness["signal_exhaustive"])
    return {
        "dates": per_date,
        "by_sector": sector_coverage,
        "signal_changes_between_exported_dates": updates,
        "coverage_interpretation": (
            "FULL_EXPORTED_UNIVERSE_AS_SELF_ATTESTED"
            if full_scope_claimed else "SAMPLE_ONLY_UNKNOWN_TRUE_UNIVERSE_COVERAGE"),
        "portfolio_interpretation": weight_validity,
        "date_count": len(dates),
        "date_range": [dates[0], dates[-1]],
        "actual_BRAIN_distribution_equivalence_verified": False,
        "concentrated_weight_RA_root_cause_established": False,
        "daily_cadence_verified": False,
    }


def audit_distribution(root, plan_id, *, evidence_dir=None, snapshot_root=None):
    root = Path(root).resolve()
    decision = decide_fields_canary(root, plan_id, snapshot_root=snapshot_root)
    if (decision.get("simulation_authorized") is not False
            or decision.get("candidate_eligible") is not False):
        raise CanaryError("source field decision must forbid live research writes")
    decision_path = _canary_path(root, plan_id).with_name(plan_id + "_round3_decision.json")
    saved = _read(decision_path)
    if saved != {key: value for key, value in decision.items()
                 if key != "decision_path"}:
        raise CanaryError("source field decision has been altered")
    directory = (Path(evidence_dir) if evidence_dir is not None else
                 root / "evidence" / "canary" / plan_id)
    if directory.is_symlink():
        raise CanaryError("evidence directory must not be a symlink")
    files = {name: directory / name for name in REQUIRED}
    manifest_path = directory / "evidence_manifest.json"
    required = ["evidence_manifest.json"] + list(REQUIRED)
    missing = [name for name in required
               if not (directory / name).is_file()]
    common = {
        "schema_version": 1,
        "plan_id": plan_id,
        "source_alpha_id": decision["source_alpha_id"],
        "source_field_decision_sha256": file_sha256(decision_path),
        "scope": dict(SCOPE),
        "field_id": FIELD,
        "required_inputs": {
            "evidence_manifest.json": {
                "schema_version": 1,
                "plan_id": plan_id,
                "alpha_id": decision["source_alpha_id"],
                "field_id": FIELD,
                "scope": dict(SCOPE),
                "source_type": "LOCAL_EXPORT",
                "completeness": {
                    "universe_complete": "boolean self-attestation",
                    "signal_exhaustive": "boolean self-attestation",
                    "weights_exhaustive": "boolean self-attestation",
                },
                "source_descriptions": {
                    name: "nonempty original export source description"
                    for name in REQUIRED},
                "files_sha256": {
                    name: "lowercase SHA-256 of CSV file bytes"
                    for name in REQUIRED},
            },
            **{name: columns for name, columns in REQUIRED.items()},
        },
        "model_invoked": False,
        "simulation_invoked": False,
        "alpha_submission_invoked": False,
        "alpha_tags_modified": False,
        "candidate_eligible": False,
        "simulation_authorized": False,
    }
    if missing:
        return {
            **common,
            "status": "DISTRIBUTION_EVIDENCE_MISSING",
            "missing_inputs": missing,
            "computed_metrics": None,
            "source_provenance_verified": False,
            "next_stage": "EXPORT_LOCAL_INSTRUMENT_DISTRIBUTIONS",
        }
    _file(manifest_path, max_bytes=64 * 1024)
    for path in files.values():
        _file(path)
    completeness, digests = _manifest(
        manifest_path, plan_id=plan_id,
        alpha_id=decision["source_alpha_id"], files=files)
    universe = _read_csv(files["universe.csv"], REQUIRED["universe.csv"])
    signal = _read_csv(files["signal.csv"], REQUIRED["signal.csv"])
    weights = _read_csv(files["weights.csv"], REQUIRED["weights.csv"])
    measured = _measure(universe, signal, weights, completeness)
    report = {
        **common,
        "status": "DISTRIBUTION_METRICS_COMPUTED_PROVENANCE_REVIEW_REQUIRED",
        "missing_inputs": [],
        "input_manifest_sha256": file_sha256(manifest_path),
        "input_file_sha256": digests,
        "manifest_completeness_self_attested": completeness,
        "computed_metrics": measured,
        "source_provenance_verified": False,
        "source_export_claims_not_independently_verified": True,
        "next_stage": "VERIFY_ORIGINAL_EXPORT_LINEAGE_AND_REVIEW_WEIGHT_CONCENTRATION",
    }
    key_data = {
        "decision": common["source_field_decision_sha256"],
        "manifest": report["input_manifest_sha256"],
        "csv": report["input_file_sha256"],
    }
    digest = hashlib.sha256(json.dumps(key_data, sort_keys=True,
                 separators=(",", ":")).encode("utf-8")).hexdigest()
    path = _canary_path(root, plan_id).with_name(
        plan_id + "_distribution_" + digest[:16] + ".json")
    if path.exists() or path.is_symlink():
        if _read(path) != report:
            raise CanaryError("existing distribution evidence report differs")
    else:
        atomic_write_json(path, report)
    return {**report, "distribution_report_path": str(path)}
