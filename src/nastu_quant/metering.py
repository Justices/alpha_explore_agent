"""Run metering (FR-15): per-batch duration, simulation counts, token
usage where obtainable. Unobtainable metrics are recorded as null and
must be reported as 「未统计」 — never estimated (NFR-06).
"""
import json
from pathlib import Path

from . import paths


def metering_path() -> Path:
    return paths.STATE_ROOT / "metering" / "metering.jsonl"


def append_record(record: dict) -> None:
    p = metering_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def read_records(path: Path = None):
    p = path or metering_path()
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()
            if ln.strip()]


def summarize(records) -> dict:
    """Aggregate batch records. token stays None (未统计） unless at
    least one record actually carries a measured value."""
    tokens = [r["token"] for r in records if r.get("token") is not None]
    return {
        "batches": len(records),
        "simulations": sum(r.get("simulations", 0) for r in records),
        "duration_s": sum(r.get("duration_s", 0) for r in records),
        "retrievals": sum(r.get("retrievals", 0) for r in records),
        "fallbacks": sum(r.get("fallbacks", 0) for r in records),
        "token": sum(tokens) if tokens else None,
    }
