"""Validate the role -> backend -> transport configuration before any model call."""
from dataclasses import dataclass
from math import isfinite
from typing import Mapping


class AgentConfigError(ValueError):
    pass


# Backend execution mode is orthogonal to research role. Codex/Claude Code
# CLIs and Antigravity SDK are agent runtimes; DeepSeek/GLM/Kimi/Claude API
# can be accessed through the model-API transport family.
# The historical agy_cli remains explicitly excluded from unattended routing.
PROVIDERS_BY_KIND = {
    "agent_runtime": frozenset({"codex_cli", "claude_cli", "antigravity_sdk"}),
    "llm_api": frozenset({"openai_compatible", "anthropic_compatible", "json_http"}),
    "mock": frozenset({"mock"}),
}
SUPPORTED_PROVIDERS = frozenset().union(*PROVIDERS_BY_KIND.values())


def backend_kind(provider):
    for kind, providers in PROVIDERS_BY_KIND.items():
        if provider in providers:
            return kind
    raise AgentConfigError(f"unsupported backend provider: {provider}")



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
            if not isinstance(name, str) or not name or not isinstance(spec, dict):
                raise AgentConfigError("backend names and definitions must be objects")
            provider = spec.get("provider")
            if provider not in SUPPORTED_PROVIDERS:
                raise AgentConfigError(f"unsupported unattended backend provider: {provider}")
            inferred_kind = backend_kind(provider)
            if spec.get("kind", inferred_kind) != inferred_kind:
                raise AgentConfigError(
                    f"backend {name}: kind and provider are incompatible")
            # Never store credentials directly in a checked-in model profile.
            if {"api_key", "access_token", "password", "client_secret", "credentials"} & set(spec):
                raise AgentConfigError("model secrets must be provided through environment variables")
            model = spec.get("model")
            if model is not None and (
                    not isinstance(model, str) or not model.strip()):
                raise AgentConfigError(f"invalid model name for backend {name}")
            if provider == "codex_cli":
                # The CLI adapter enforces sandbox=read-only and approval=never;
                # unsupported overrides must fail, not be silently ignored.
                allowed = {
                    "provider", "kind", "executable", "model", "timeout_seconds",
                    "sandbox", "session",
                }
                if (set(spec) - allowed
                        or spec.get("sandbox", "read-only") != "read-only"
                        or spec.get("session", "research") not in {"research", "ephemeral"}):
                    raise AgentConfigError(
                        "Codex research backend requires read-only sandbox and a valid session mode")
            if provider == "claude_cli":
                # Claude Code is an agent runtime, not the Anthropic Messages API.
                # The existing CLI adapter explicitly disables tools/MCP and
                # session persistence. Do not silently accept unsafe overrides.
                allowed = {"provider", "kind", "executable", "model", "timeout_seconds", "session"}
                if (set(spec) - allowed
                        or spec.get("session", "ephemeral") != "ephemeral"):
                    raise AgentConfigError(
                        "Claude Code CLI backend is supported only with isolated ephemeral sessions")
            timeout = spec.get("timeout_seconds", 180)
            if (type(timeout) not in (int, float) or
                    not isfinite(timeout) or timeout <= 0):
                raise AgentConfigError(f"invalid timeout for backend {name}")
            if provider == "antigravity_sdk":
                key_env = spec.get("api_key_env")
                if not isinstance(key_env, str) or not key_env:
                    raise AgentConfigError("antigravity_sdk requires api_key_env")
                if (spec.get("tools") or spec.get("mcp_servers")
                        or spec.get("skills_paths") or spec.get("allow_write")):
                    raise AgentConfigError("unattended antigravity_sdk tools are disabled in P0")
                allowed = {
                    "provider", "kind", "model", "timeout_seconds", "api_key_env",
                    "tools", "mcp_servers", "skills_paths", "allow_write",
                }
                if set(spec) - allowed:
                    raise AgentConfigError("unsupported Antigravity SDK backend option")
            if provider in {"openai_compatible", "anthropic_compatible", "json_http"}:
                url = spec.get("url")
                if not isinstance(url, str) or not url.startswith("https://"):
                    raise AgentConfigError(
                        "model API backends require an explicit HTTPS URL")
                key_env = spec.get("api_key_env")
                if provider != "json_http" and (
                        not isinstance(key_env, str) or not key_env):
                    raise AgentConfigError(
                        f"{provider} requires an API credential environment variable")
                if key_env is not None and (
                        not isinstance(key_env, str) or not key_env.strip()):
                    raise AgentConfigError("api_key_env must be a nonempty string")
                if provider != "json_http" and (
                        not isinstance(spec.get("model"), str)
                        or not spec["model"].strip()):
                    raise AgentConfigError(f"{provider} requires a model identifier")
                allowed = {"provider", "kind", "model", "url", "api_key_env", "timeout_seconds"}
                if provider == "openai_compatible":
                    allowed.add("response_format")
                    if spec.get("response_format", "json_object") not in {
                            "json_object", "none"}:
                        raise AgentConfigError(
                            "OpenAI-compatible response_format must be json_object or none")
                if set(spec) - allowed:
                    raise AgentConfigError(
                        f"unsupported model API configuration keys for {provider}")
            if provider == "mock":
                if set(spec) - {"provider", "kind", "responses", "timeout_seconds"}:
                    raise AgentConfigError("unsupported mock backend option")
                responses = spec.get("responses", {})
                if not isinstance(responses, dict) or any(
                    not isinstance(reply, dict) and not (
                        isinstance(reply, list) and reply
                        and all(isinstance(entry, dict) for entry in reply))
                    for reply in responses.values()
                ):
                    raise AgentConfigError("mock responses must be JSON objects or nonempty object lists")
        for role, definition in roles.items():
            if not isinstance(role, str) or not role or not isinstance(definition, dict):
                raise AgentConfigError("every role needs an object definition")
            name = definition.get("backend")
            if name not in backends:
                raise AgentConfigError(f"role {role} references undefined backend {name}")
        for task, role in task_roles.items():
            if not isinstance(task, str) or not task or role not in roles:
                raise AgentConfigError("task_roles must map tasks to defined roles")
        return cls(default, dict(task_roles), dict(roles), dict(backends))

    def resolve(self, task: str) -> RoleBinding:
        if not isinstance(task, str) or not task:
            raise AgentConfigError("model task must be a nonempty string")
        role = self.task_roles.get(task, self.default_role)
        name = self.roles[role]["backend"]
        return RoleBinding(role, name, dict(self.backends[name]))
