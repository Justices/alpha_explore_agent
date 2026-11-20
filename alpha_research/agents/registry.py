"""Preset registry for LLM APIs and Agent runtimes.

Selection changes the role->backend binding without touching research workflow
logic. Only selected entries are materialized in the effective model config.
Registry listing and inspection never contact external providers.
"""
import copy
import json
from pathlib import Path

from alpha_research.agents.configuration import (
    AgentConfigError, RoleRoutingConfig, backend_kind,
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
        # Reuse the authoritative provider validation, including tool privilege
        # restrictions and credential environment variable checks.
        RoleRoutingConfig.parse({
            "provider": "role_router", "default_role": "registered",
            "roles": {"registered": {"backend": name}},
            "backends": {name: spec},
        })


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
    """Return an isolated validated role-router config, never mutate inputs.

    Example: assignments={"research_lead":"codex", "research_executor":"deepseek"}.
    A registration may be selected by multiple roles; each role remains
    logically isolated in research and audit metadata.
    """
    if not isinstance(model_config, dict) or model_config.get("provider") != "role_router":
        raise ModelRegistryError("role selection requires a role_router model configuration")
    if not isinstance(registry, dict) or registry.get("schema_version") != 1:
        raise ModelRegistryError("invalid registry")
    entries = registry.get("entries")
    _validate_entries(entries)
    if not isinstance(assignments, dict) or not assignments:
        raise ModelRegistryError("role assignments are required")
    selected = copy.deepcopy(model_config)
    selected.setdefault("roles", {})
    selected.setdefault("backends", {})
    for role, registration in assignments.items():
        if not isinstance(role, str) or role not in selected["roles"]:
            raise ModelRegistryError(f"unknown research role: {role}")
        if not isinstance(registration, str) or registration not in entries:
            raise ModelRegistryError(f"unregistered backend: {registration}")
        # Never overwrite a legacy project backend of the same identifier:
        # role bindings get an isolated registry namespace.
        key = "registry:" + registration
        selected["backends"][key] = copy.deepcopy(entries[registration])
        selected["roles"][role]["backend"] = key
    RoleRoutingConfig.parse(selected)
    return selected
