"""Shared CLI model-runtime configuration assembly; no I/O to providers.

Use the same registry and role-profile contract for live CLI operations and
orchestration task preparation. This module does not create a Gateway, acquire
credentials, reserve a model call, or authorize Simulation.
"""
from alpha_research.agents.registry import (
    load_role_profile, load_registry, select_registered_backends,
    assert_live_model_routes, ModelRegistryError,
)
from alpha_research.agents.governance import validate_policy


def prepare_registered_model_runtime(config, assignments, registry_path):
    """Return a fresh configuration with validated model profile and policy.

    Both roles must be explicitly selected. No fallback to legacy inline
    business-config backends is permitted.
    """
    if not isinstance(config, dict):
        raise ModelRegistryError("research config must be an object")
    if (not isinstance(assignments, dict)
            or set(assignments) != {"research_lead", "research_executor"}
            or not all(isinstance(x, str) and x for x in assignments.values())):
        raise ModelRegistryError("both registered Lead and Executor are required")
    policy = validate_policy(config.get("model_governance"))
    profile = select_registered_backends(
        load_role_profile(), load_registry(registry_path),
        assignments=assignments)
    assert_live_model_routes(profile)
    result = dict(config)
    result["model"] = profile
    result["model_governance"] = policy
    return result


# Only for offline task preparation. Never auto-injected into live model calls.
OFFLINE_PREPARATION_POLICY = {
    "max_calls": 1,
    "max_reported_tokens": 4096,
    "failover": "manual",
}


def load_preparation_config(config_path, *, explicit=False):
    """Load user configuration, or conservative built-ins for an absent default.

    Missing explicit configuration, malformed JSON, and present-but-invalid
    model_governance always fail. No file is created and no live run inherits
    this fallback implicitly.
    """
    import json
    from copy import deepcopy
    from pathlib import Path
    from alpha_research.config import DEFAULT_CONFIG

    path = Path(config_path)
    if not path.is_file():
        if explicit or path.exists():
            raise FileNotFoundError(
                f"research configuration file not found: {path}")
        cfg = deepcopy(DEFAULT_CONFIG)
        cfg["model_governance"] = dict(OFFLINE_PREPARATION_POLICY)
        return cfg, "builtin_offline_defaults"
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"research configuration is not valid JSON: {path}") from exc
    if not isinstance(cfg, dict):
        raise ValueError(f"research configuration must be an object: {path}")
    if "model_governance" not in cfg:
        raise ValueError(
            "research configuration is missing model_governance "
            "(max_calls, max_reported_tokens, failover)")
    validate_policy(cfg["model_governance"])
    return cfg, "user_config"
