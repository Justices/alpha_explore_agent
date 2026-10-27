"""Explicit configuration (AGENT_DESIGN.md §8, NFR-05).

One JSON config file inside the state root's config/ dir, plus a
human-readable change log. Scope-class changes require user approval;
that approval workflow lives in the harness — this module only stores
values and records changes with operator/time/reason.
"""
import json
from datetime import datetime
from pathlib import Path

from . import paths
from .state_store import atomic_write_json, read_json

DEFAULT_CONFIG = {
    "scope": {
        "region": None,
        "dataset_id": None,
        "universe": None,
        "delay": None,
        "decay": None,
        "neutralization": None,
        "truncation": None,
        "pasteurization": None,
        "unit_handling": None,
        "nan_handling": None,
        "test_period": None,
        "max_trade": None,
        "is_start": None,
        "is_end": None,
    },
    "gate": {
        "sharpe_min": 1.58,
        "china_sharpe_min": 2.08,
        "fitness_min": 1.0,
        "two_year_sharpe_min": 1.60,
        "margin_min": 0.0005,
        "turnover_min": 0.05,
        "turnover_max": 0.30,
        "prod_corr_max": 0.70,       # current rule: inclusive
        "self_corr_max": 0.70,       # current rule: inclusive
        "pairwise_corr_max": 0.40,   # strict less-than
    },
    "probe": {"windows": [5, 66]},
    "exec": {"multisim_wait_threshold_s": 1200},
    "auth": {"mode": "auto", "credential_env": "NASTU_QUANT_CREDENTIAL_FILE"},
    "model": {"provider": None, "url": None, "model": None,
              "api_key_env": "NASTU_QUANT_MODEL_API_KEY"},
}

SCOPE_KEYS = {"scope", "gate"}  # changes to these sections need approval


def config_path() -> Path:
    return paths.STATE_ROOT / "config" / "nastu_quant.json"


def change_log_path() -> Path:
    return paths.STATE_ROOT / "config" / "change_log.md"


def load_config() -> dict:
    cfg = read_json(config_path())
    if cfg is None:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    atomic_write_json(config_path(), cfg)


def record_change(operator: str, summary: str, reason: str) -> None:
    log = change_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log.open("a", encoding="utf-8") as f:
        f.write(f"- {stamp} | {operator} | {summary} | reason: {reason}\n")


def backtest_settings(cfg: dict) -> dict:
    """The settings dict handed to simulation calls, from scope config."""
    s = cfg["scope"]
    return {
        "region": s["region"],
        "universe": s["universe"],
        "delay": s["delay"],
        "decay": s["decay"],
        "neutralization": s["neutralization"],
        "truncation": s["truncation"],
        "pasteurization": s["pasteurization"],
        "start_date": s["is_start"],
        "end_date": s["is_end"],
    }
