"""Admission control for CLI Agent, SDK Agent and LLM API backends.

This module owns backend capabilities, provider families and safety policy.
No model provider, SDK or BRAIN endpoint is imported or invoked here.
"""
from math import isfinite
from urllib.parse import urlsplit


class AgentConfigError(ValueError):
    pass


# Backend execution mode is orthogonal to research role. Codex/Claude Code
# CLIs and Antigravity SDK are agent runtimes; DeepSeek/GLM/Kimi/Claude API
# can be accessed through the model-API transport family.
# The historical agy_cli remains explicitly excluded from unattended routing.
PROVIDERS_BY_KIND = {
    "agent_runtime": frozenset({"codex_cli", "claude_cli", "antigravity_sdk"}),
    "llm_api": frozenset({"openai_sdk"}),
    "mock": frozenset({"mock"}),
}
SUPPORTED_PROVIDERS = frozenset().union(*PROVIDERS_BY_KIND.values())


def backend_kind(provider):
    for kind, providers in PROVIDERS_BY_KIND.items():
        if isinstance(provider, str) and provider in providers:
            return kind
    raise AgentConfigError(f"unsupported backend provider: {provider}")


def validate_backend(name, spec):
    """Validate one named registry/backend definition before routing."""
    if not isinstance(name, str) or not name or not isinstance(spec, dict):
        raise AgentConfigError("backend names and definitions must be objects")
    provider = spec.get("provider")
    if not isinstance(provider, str) or provider not in SUPPORTED_PROVIDERS:
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
    if provider in {"codex_cli", "claude_cli"}:
        executable = spec.get("executable")
        if executable is not None and (
                not isinstance(executable, str) or not executable.strip()):
            raise AgentConfigError(f"invalid CLI executable for backend {name}")
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
        if not isinstance(key_env, str) or not key_env.strip():
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
    if provider == "openai_sdk":
        # Only OpenAI Chat Completions compatible HTTPS endpoints can use
        # this adapter. The SDK appends /chat/completions to base_url.
        base_url = spec.get("base_url")
        try:
            parsed_url = urlsplit(base_url) if isinstance(base_url, str) else None
            valid_url = bool(
                parsed_url and parsed_url.scheme == "https"
                and parsed_url.hostname and parsed_url.username is None
                and parsed_url.password is None
                and not parsed_url.fragment and not parsed_url.query
                and not parsed_url.path.rstrip("/").endswith("/chat/completions")
            )
        except ValueError:
            valid_url = False
        if not valid_url:
            raise AgentConfigError("openai_sdk requires an HTTPS API base_url")
        key_env = spec.get("api_key_env")
        if not isinstance(key_env, str) or not key_env.strip():
            raise AgentConfigError("openai_sdk requires api_key_env")
        if not isinstance(spec.get("model"), str) or not spec["model"].strip():
            raise AgentConfigError("openai_sdk requires model")
        response_format = spec.get("response_format", "json_object")
        if response_format not in {"json_object", "none"}:
            raise AgentConfigError(
                "OpenAI SDK response_format must be json_object or none")
        allowed = {"provider", "kind", "model", "base_url",
                   "api_key_env", "timeout_seconds", "response_format"}
        if set(spec) - allowed:
            raise AgentConfigError("unsupported OpenAI SDK backend option")
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
