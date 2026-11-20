"""Read-only, API-key-authenticated Google Antigravity SDK model adapter.

The SDK is optional (pip install google-antigravity). This adapter does NOT
expose tools, MCP servers, filesystem mutation or BRAIN operations.
The research gateway separately owns all non-idempotent simulation intents.
"""
import asyncio
import json
import os
import re


class AntigravityAdapterError(RuntimeError):
    pass


def decode_json_object(message: str) -> dict:
    if not isinstance(message, str):
        raise AntigravityAdapterError("AGY returned a non-text response")
    try:
        result = json.loads(message)
    except ValueError:
        blocks = re.findall(r"```(?:json)?\s*(.*?)\s*```", message,
                            flags=re.IGNORECASE | re.DOTALL)
        if len(blocks) != 1:
            raise AntigravityAdapterError("AGY response must be one JSON object")
        try:
            result = json.loads(blocks[0])
        except ValueError as exc:
            raise AntigravityAdapterError("AGY response is invalid JSON") from exc
    if not isinstance(result, dict):
        raise AntigravityAdapterError("AGY response must be a JSON object")
    return result


class AntigravityModelClient:
    def __init__(self, config, environ=None, agent_factory=None):
        self.config = dict(config)
        self.environ = os.environ if environ is None else environ
        self.agent_factory = agent_factory
        self.last_usage = None
        self.last_model = None
        self.last_response_text = None
        self.last_thread_id = None

    def build_local_config(self, secret):
        """Construct read-only SDK configuration without starting an Agent."""
        try:
            from google.antigravity import LocalAgentConfig, types
            from google.antigravity.hooks import policy
        except ImportError as exc:
            raise AntigravityAdapterError(
                "install optional dependency google-antigravity for AGY SDK") from exc
        # Instructions are not a security boundary; constrain both tool
        # availability and runtime authorization. No BRAIN gateway is exposed.
        kwargs = {
            "system_instructions": (
                "You analyze supplied Alpha research evidence. Return one JSON "
                "object. Never infer that a simulation ran or a candidate qualified."
            ),
            "env": {"GEMINI_API_KEY": secret},
            "tools": [],
            "mcp_servers": [],
            "skills_paths": [],
            "capabilities": types.CapabilitiesConfig(
                enabled_tools=[types.BuiltinTools.FINISH],
                enable_subagents=False,
            ),
            "policies": [policy.deny("*"), policy.allow("finish")],
        }
        if self.config.get("model"):
            kwargs["model"] = self.config["model"]
        return LocalAgentConfig(**kwargs)

    async def _run(self, prompt, timeout, secret):
        try:
            from google.antigravity import Agent
        except ImportError as exc:
            raise AntigravityAdapterError(
                "install optional dependency google-antigravity for AGY SDK") from exc
        agent_config = self.build_local_config(secret)
        factory = self.agent_factory or Agent

        async def interact():
            async with factory(agent_config) as agent:
                response = await agent.chat(prompt)
                return await response.text()
        return await asyncio.wait_for(interact(), timeout=timeout)

    def complete_json(self, task, context, *, thread_path=None):
        if thread_path is not None:
            raise AntigravityAdapterError("AGY SDK does not accept Codex thread bindings")
        key_name = self.config.get("api_key_env")
        if not isinstance(key_name, str) or not key_name:
            raise AntigravityAdapterError("AGY SDK requires api_key_env")
        secret = self.environ.get(key_name)
        if not secret:
            raise AntigravityAdapterError(f"missing AGY SDK credential: {key_name}")
        timeout = self.config.get("timeout_seconds", 180)
        if type(timeout) not in (int, float) or timeout <= 0:
            raise AntigravityAdapterError("AGY SDK timeout must be positive")
        prompt = json.dumps({
            "task": task, "context": context,
            "instruction": (
                "Return exactly one JSON object. Analyze provided evidence only. "
                "Never call tools or claim to have performed simulations."
            ),
        }, ensure_ascii=False, allow_nan=False)
        self.last_usage = None
        self.last_thread_id = None
        self.last_model = self.config.get("model")
        self.last_response_text = None
        try:
            message = asyncio.run(self._run(prompt, timeout, secret))
            self.last_response_text = message
            return decode_json_object(message)
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise AntigravityAdapterError("AGY SDK request timed out") from exc
