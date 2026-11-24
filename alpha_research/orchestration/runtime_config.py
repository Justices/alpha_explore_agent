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
from alpha_research.config import load_config


# Strictly isolated synthetic Super/Optimization domain mock only. This is not
# a fallback for user config and is never used by live model execution.
OFFLINE_PREPARATION_POLICY = {
    "max_calls": 1,
    "max_reported_tokens": 4096,
    "failover": "manual",
}


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


def load_preparation_config(config_path, *, explicit=False):
    """Load the same canonical config used by preflight and live runs.

    A missing default State Root config is initialized automatically. An
    explicitly requested missing path still fails closed. Only Scope survives
    loading; all policy sections come from application defaults.
    """
    from pathlib import Path

    path = Path(config_path)
    missing = not path.is_file()
    if missing and explicit:
        raise FileNotFoundError(
            f"research configuration file not found: {path}")
    cfg = load_config(path, initialize=not explicit)
    return cfg, "system_defaults_initialized" if missing else "state_config"
