"""System-owned research defaults with a user-selectable research Scope.

Only the scope section is configurable. Gate thresholds, preference
criteria, authentication mechanism, knowledge layout and model governance are
owned by the application and reconstructed from DEFAULT_CONFIG on load.
"""
import copy
import json
import math
from datetime import datetime
from pathlib import Path

from alpha_research.state import paths
from alpha_research.state.store import atomic_write_json
from alpha_research.agents.registry import load_role_profile


DEFAULT_CONFIG = {
    "scope": {
        "region": None,
        "dataset_id": None,
        "category": None,
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
        "risk_neutralized_sharpe_min": 1.0,
        "risk_neutralized_fitness_min": 0.7,
        "turnover_min": 0.05,
        "turnover_max": 0.30,
        "prod_corr_max": 0.70,
        "self_corr_max": 0.70,
        "pairwise_corr_max": 0.40,
    },
    "preferred": {
        "sharpe_min": 1.25,
        "fitness_min": 1.0,
        "turnover_min": 0.01,
        "turnover_max": 0.70,
        "weight_max": 0.10,
        "require_sub_universe_pass": True,
    },
    "probe": {"windows": [5, 66]},
    "exec": {"multisim_wait_threshold_s": 1200},
    "auth": {"mode": "auto", "credential_env": "BRAIN_CREDENTIALS_FILE"},
    "model": load_role_profile(),
    "knowledge": {
        "construction_advisory_path": None,
        "prior_knowledge_path": None,
        "operator_root": None,
        "field_root": None,
    },
    "model_governance": {
        "max_calls": 80,
        "max_reported_tokens": 250000,
        "failover": "manual",
    },
}

# The Scope is the only user-controlled configuration section.
SCOPE_KEYS = {"scope"}


class ConfigError(ValueError):
    """The persisted research Scope is malformed or unsupported."""


def default_config() -> dict:
    """Return an isolated copy of the canonical system defaults."""
    return copy.deepcopy(DEFAULT_CONFIG)


def config_path(state_root=None) -> Path:
    root = Path(state_root) if state_root is not None else paths.STATE_ROOT
    return root / "config" / "nastu_quant.json"


def change_log_path(state_root=None) -> Path:
    root = Path(state_root) if state_root is not None else paths.STATE_ROOT
    return root / "config" / "change_log.md"


def _canonical_config(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ConfigError("research configuration must be a JSON object")
    scope = raw.get("scope")
    if scope is None:
        scope = {}
    if not isinstance(scope, dict):
        raise ConfigError("research configuration scope must be an object")
    unknown = set(scope) - set(DEFAULT_CONFIG["scope"])
    if unknown:
        raise ConfigError(
            "unsupported research scope keys: " + ", ".join(sorted(unknown)))
    for key, value in scope.items():
        if isinstance(value, (dict, list)):
            raise ConfigError(f"research scope value must be scalar: {key}")
        if isinstance(value, float) and not math.isfinite(value):
            raise ConfigError(f"research scope value must be finite: {key}")
    effective = default_config()
    effective["scope"].update(scope)
    return effective


def save_config(cfg: dict, path=None) -> None:
    """Persist Scope over canonical system defaults; fixed sections are ignored."""
    target = Path(path) if path is not None else config_path()
    if target.is_symlink() or target.parent.is_symlink():
        raise ConfigError("research configuration symlinks are forbidden")
    atomic_write_json(target, _canonical_config(cfg))


def load_config(path=None, *, initialize=True) -> dict:
    """Load effective config and initialize/migrate system defaults.

    A missing default config is created automatically. An explicitly supplied
    path can opt out of initialization. Existing fixed sections are never
    trusted: only Scope is retained and all other sections come from the
    canonical application defaults. Invalid JSON or Scope fails closed.
    """
    target = Path(path) if path is not None else config_path()
    if target.is_symlink() or target.parent.is_symlink():
        raise ConfigError("research configuration symlinks are forbidden")
    if not target.is_file():
        if target.exists() or not initialize:
            raise FileNotFoundError(f"research configuration file not found: {target}")
        effective = default_config()
        save_config(effective, target)
        return effective
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"research configuration is unreadable: {target}") from exc
    effective = _canonical_config(raw)
    if raw != effective:
        # Deterministic migration: preserve Scope and restore application-owned
        # sections. No user-authored fixed setting is used.
        save_config(effective, target)
    return effective


def record_change(operator: str, summary: str, reason: str) -> None:
    log = change_log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log.open("a", encoding="utf-8") as handle:
        handle.write(f"- {stamp} | {operator} | {summary} | reason: {reason}\n")


def backtest_settings(cfg: dict) -> dict:
    """The settings dict handed to simulation calls, from the selected Scope."""
    scope = cfg["scope"]
    return {
        "region": scope["region"],
        "universe": scope["universe"],
        "delay": scope["delay"],
        "decay": scope["decay"],
        "neutralization": scope["neutralization"],
        "truncation": scope["truncation"],
        "pasteurization": scope["pasteurization"],
        "start_date": scope["is_start"],
        "end_date": scope["is_end"],
    }
