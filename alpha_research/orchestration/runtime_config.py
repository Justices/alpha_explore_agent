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
