"""Validate the role -> backend -> transport configuration before any model call."""
import copy
from dataclasses import dataclass
from typing import Mapping

from alpha_research.agents.backend_validation import (
    AgentConfigError, PROVIDERS_BY_KIND, SUPPORTED_PROVIDERS,
    backend_kind, validate_backend,
)


@dataclass(frozen=True)
class RoleBinding:
    role: str
    backend_name: str
    backend: dict


@dataclass(frozen=True)
class RoleRoutingConfig:
    default_role: str
    task_roles: dict
    roles: dict
    backends: dict

    @classmethod
    def parse(cls, config: Mapping):
        if not isinstance(config, dict) or config.get("provider") != "role_router":
            raise AgentConfigError("role router requires provider=role_router")
        default = config.get("default_role")
        roles = config.get("roles")
        backends = config.get("backends")
        task_roles = config.get("task_roles", {})
        if not isinstance(default, str) or not isinstance(roles, dict) or default not in roles:
            raise AgentConfigError("default_role must name a defined role")
        if not isinstance(backends, dict) or not backends:
            raise AgentConfigError("backends must be a nonempty object")
        if not isinstance(task_roles, dict):
            raise AgentConfigError("task_roles must be an object")
        for name, spec in backends.items():
            validate_backend(name, spec)
        for role, definition in roles.items():
            if not isinstance(role, str) or not role or not isinstance(definition, dict):
                raise AgentConfigError("every role needs an object definition")
            name = definition.get("backend")
            if not isinstance(name, str) or name not in backends:
                raise AgentConfigError(f"role {role} references undefined backend {name}")
        for task, role in task_roles.items():
            if (not isinstance(task, str) or not task
                    or not isinstance(role, str) or role not in roles):
                raise AgentConfigError("task_roles must map tasks to defined roles")
        return cls(default, copy.deepcopy(task_roles), copy.deepcopy(roles),
                   copy.deepcopy(backends))

    def resolve(self, task: str) -> RoleBinding:
        if not isinstance(task, str) or not task:
            raise AgentConfigError("model task must be a nonempty string")
        role = self.task_roles.get(task, self.default_role)
        name = self.roles[role]["backend"]
        # Do not leak nested registry data through a mutable route binding.
        # In particular, mock response fixtures must not be caller-mutable.
        return RoleBinding(role, name, copy.deepcopy(self.backends[name]))
