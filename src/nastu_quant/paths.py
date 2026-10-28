"""Path and naming rules (AGENT_DESIGN.md §5.1 / §5.2, NFR-07).

Runtime state lives in the operating system's per-user state directory by
default, outside this repository. ``NASTU_QUANT_HOME`` can override it.
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    TZ = ZoneInfo("Asia/Shanghai")
except ZoneInfoNotFoundError:  # Windows Python may not ship an IANA time-zone database.
    TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")


def expand_path(value) -> Path:
    """Expand a user path consistently on Windows, macOS, and Linux."""
    raw = os.path.expandvars(os.fspath(value))
    # Also accept Windows-style %NAME% values in config files used on POSIX.
    raw = re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%",
                 lambda match: os.environ.get(match.group(1), match.group(0)), raw)
    return Path(raw).expanduser()


def default_state_root() -> Path:
    """Return a per-user state location following each OS's conventions."""
    configured = os.environ.get("NASTU_QUANT_HOME")
    if configured:
        return expand_path(configured).resolve()

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return (expand_path(base) / "nastu_quant").resolve()


STATE_ROOT = default_state_root()

PRODUCT_TYPES = {
    "probe_report", "prereg", "verdicts",
    "results_raw", "results", "gate", "metering",
}
FILE_FORMATS = {"md", "json", "jsonl"}


def state_dir() -> Path:
    return STATE_ROOT / "state"


def runs_root() -> Path:
    return STATE_ROOT / "runs"


def dataset_dir(region: str, delay, universe: str, datasetid: str) -> Path:
    """runs/{region}/{delay}/{universe}/{datasetid}/ (NFR-07)."""
    return runs_root() / region / f"D{delay}" / universe / datasetid


def batch_timestamp(when: datetime = None) -> str:
    """Batch time prefix YYYYMMDD_HHMMSS (Asia/Shanghai)."""
    when = when or datetime.now(TZ)
    return when.strftime("%Y%m%d_%H%M%S")


def batch_filename(timestamp: str, fields, product_type: str, fmt: str) -> str:
    """{时间}_{字段组合}_{产物类型}.{格式} — files of one batch share
    the same timestamp prefix and form the batch file group (§5.2)."""
    if product_type not in PRODUCT_TYPES:
        raise ValueError(f"unknown product type: {product_type}")
    if fmt not in FILE_FORMATS:
        raise ValueError(f"unknown file format: {fmt}")
    if not re.fullmatch(r"\d{8}_\d{6}", timestamp):
        raise ValueError(f"bad batch timestamp: {timestamp}")
    field_part = "_".join(fields)
    return f"{timestamp}_{field_part}_{product_type}.{fmt}"


def batch_file(region, delay, universe, datasetid,
               timestamp, fields, product_type, fmt) -> Path:
    return dataset_dir(region, delay, universe, datasetid) / batch_filename(
        timestamp, fields, product_type, fmt)
