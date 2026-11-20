"""Role-to-model mappings for the canonical alpha_research models inspect CLI."""

from alpha_research.agents.configuration import RoleRoutingConfig, backend_kind


def inspect_roles(model_config, tasks):
    routing = RoleRoutingConfig.parse(model_config)
    roles = {}
    for role, definition in sorted(routing.roles.items()):
        name = definition["backend"]
        backend = routing.backends[name]
        roles[role] = {
            "backend": name,
            "provider": backend["provider"],
            "kind": backend_kind(backend["provider"]),
            "model": backend.get("model"),
            "timeout_seconds": backend.get("timeout_seconds", 180),
            "session": backend.get("session") if backend["provider"] == "codex_cli" else None,
            # Environment variable *name* only; no secret or environment value.
            "api_key_env": backend.get("api_key_env"),
        }
    task_names = sorted(set(tasks) if tasks else set(routing.task_roles))
    task_routes = {}
    for task in task_names:
        binding = routing.resolve(task)
        task_routes[task] = {
            "role": binding.role,
            "backend": binding.backend_name,
            "provider": binding.backend["provider"],
            "kind": backend_kind(binding.backend["provider"]),
        }
    return {
        "default_role": routing.default_role,
        "roles": roles,
        "task_routes": task_routes,
        "validated": True,
        "provider_invoked": False,
    }
