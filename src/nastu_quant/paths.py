"""Path and naming rules (AGENT_DESIGN.md §5.1 / §5.2, NFR-07).

State root lives OUTSIDE this code repository (default
~/workspace/nastu_quant) so runtime state — above all the session
cookie — is never committed to version control.
"""
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Shanghai")

STATE_ROOT = Path(os.environ.get(
    "NASTU_QUANT_HOME", str(Path.home() / "workspace" / "nastu_quant")))

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
