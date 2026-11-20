"""Preset registry for LLM APIs and Agent runtimes.

Selection changes the role->backend binding without touching research workflow
logic. Only selected entries are materialized in the effective model config.
Registry listing and inspection never contact external providers.
"""
import copy
import json
from pathlib import Path

from alpha_research.agents.configuration import (
    AgentConfigError, RoleRoutingConfig, backend_kind, validate_backend,
)


class ModelRegistryError(AgentConfigError):
    pass


def _validate_entries(entries):
    if not isinstance(entries, dict) or not entries:
        raise ModelRegistryError("registry entries must be a nonempty object")
    for name, spec in entries.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(spec, dict):
            raise ModelRegistryError("registry requires named backend objects")
        provider = spec.get("provider")
        if not isinstance(provider, str):
            raise ModelRegistryError(f"registry entry {name} has no provider")
        try:
            expected_kind = backend_kind(provider)
        except AgentConfigError as exc:
            raise ModelRegistryError(f"registry entry {name}: {exc}") from exc
        if spec.get("kind") != expected_kind:
            raise ModelRegistryError(f"registry entry {name} has invalid kind for {provider}")
        # Validate the individual backend directly. No synthetic role-router
        # configuration is needed when loading registry entries.
        try:
            validate_backend(name, spec)
        except AgentConfigError as exc:
            raise ModelRegistryError(
                f"invalid registry backend {name}: {exc}") from exc


def load_registry(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModelRegistryError("model registry cannot be read as JSON") from exc
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1
            or set(value) != {"schema_version", "entries"}):
        raise ModelRegistryError("model registry requires schema_version=1 and entries")
    _validate_entries(value["entries"])
    return copy.deepcopy(value)


def describe_registry(registry):
    """List metadata without exposing secrets or copying arbitrary configs."""
    if not isinstance(registry, dict) or registry.get("schema_version") != 1:
        raise ModelRegistryError("invalid registry")
    entries = registry.get("entries")
    _validate_entries(entries)
    return {
        "schema_version": 1,
        "entries": [
            {
                "id": name, "kind": item["kind"],
                "provider": item["provider"],
                "model": item.get("model"),
                "api_key_env": item.get("api_key_env"),
            }
            for name, item in sorted(entries.items())
        ],
        "provider_invoked": False,
    }


def select_registered_backends(model_config, registry, *, assignments):
    """Compile a role profile and selected registry IDs into a runnable model config.

    The registry is the SINGLE source of backend definitions. Any old backend
    declarations inside the model profile are discarded, not merged or copied.
    Both required roles must be selected explicitly; no silent provider fallback.
    """
    if not isinstance(model_config, dict) or model_config.get("provider") != "role_router":
        raise ModelRegistryError("role selection requires a role_router profile")
    if not isinstance(registry, dict) or registry.get("schema_version") != 1:
        raise ModelRegistryError("invalid registry")
    entries = registry.get("entries")
    _validate_entries(entries)
    roles = model_config.get("roles")
    if not isinstance(roles, dict) or not roles:
        raise ModelRegistryError("role profile requires roles")
    if not isinstance(assignments, dict) or set(assignments) != set(roles):
        raise ModelRegistryError("every configured role requires one registered backend selection")
    if "backends" in model_config:
        raise ModelRegistryError(
            "model profile must declare only roles and tasks; backends belong in registry")
    effective = copy.deepcopy(model_config)
    effective["backends"] = {}
    for role in sorted(roles):
        registration = assignments[role]
        if not isinstance(registration, str) or registration not in entries:
            raise ModelRegistryError(f"unregistered backend for {role}: {registration}")
        key = "registry:" + registration
        effective["backends"][key] = copy.deepcopy(entries[registration])
        effective["roles"][role]["backend"] = key
    RoleRoutingConfig.parse(effective)
    return effective


def bundled_registry_path():
    """Bundled fallback; operators may override with --registry."""
    return Path(__file__).resolve().parent / "model_registry.json"

def bundled_profile_path():
    """One packaged source for all default task/role declarations."""
    return Path(__file__).resolve().parent / "role_profile.json"


def load_role_profile(config=None):
    """Resolve a profile from business config; never silently merge backends.

    A model profile may be explicitly supplied for tests, but a business
    configuration normally contains no model section at all.
    """
    if config is not None and not isinstance(config, dict):
        raise ModelRegistryError("research config must be an object")
    supplied = config.get("model") if config is not None else None
    if supplied is None:
        try:
            supplied = json.loads(bundled_profile_path().read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ModelRegistryError("bundled model role profile unavailable") from exc
    if not isinstance(supplied, dict):
        raise ModelRegistryError("model role profile must be an object")
    return copy.deepcopy(supplied)

