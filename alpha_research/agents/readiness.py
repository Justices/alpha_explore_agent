"""One local readiness contract shared by Workbench and CLI preflight.

Neither registry resolution nor local readiness checks invoke a model or BRAIN.
"""
import hashlib

from alpha_research.agents.doctor import check_registered_roles
from alpha_research.agents.registry import (
    assert_live_model_routes, load_registry, load_role_profile,
    select_registered_backends,
)
from alpha_research.state.fingerprint import canonical_json_bytes


def inspect_live_model_roles(config, registry_path, *, lead, executor,
                             environment=None):
    if (not isinstance(lead, str) or not lead.strip()
            or not isinstance(executor, str) or not executor.strip()):
        raise ValueError("both registered Lead and Executor IDs are required")
    # Explicit registrations always use the canonical task/role profile.
    # Legacy business-config model blocks must not override model routing.
    effective = select_registered_backends(
        load_role_profile(), load_registry(registry_path),
        assignments={"research_lead": lead, "research_executor": executor})
    assert_live_model_routes(effective)
    local = check_registered_roles(effective, environ=environment)
    return {
        **local,
        "profile_sha256": hashlib.sha256(
            canonical_json_bytes(effective)).hexdigest(),
    }
