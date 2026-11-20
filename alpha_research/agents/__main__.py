"""Validate and inspect model role routing without calling model providers.

Usage: python -m alpha_research.agents --config config.example.json \
       --task plan_experiments --task prepare_execution
"""
import argparse
import json
from pathlib import Path

from alpha_research.agents.configuration import RoleRoutingConfig


def inspect_roles(model_config, tasks):
    routing = RoleRoutingConfig.parse(model_config)
    roles = {}
    for role, definition in sorted(routing.roles.items()):
        name = definition["backend"]
        backend = routing.backends[name]
        roles[role] = {
            "backend": name,
            "provider": backend["provider"],
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
        }
    return {
        "default_role": routing.default_role,
        "roles": roles,
        "task_routes": task_routes,
        "validated": True,
        "provider_invoked": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Inspect configurable Codex/AGY research routes without network access"
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--task", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        content = json.loads(args.config.read_text(encoding="utf-8"))
        model = content.get("model", content)
        report = inspect_roles(model, args.task)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        parser.error(f"configuration invalid: {exc}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
