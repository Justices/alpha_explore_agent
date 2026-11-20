"""Contract-only SDK smoke test: no Agent startup, credentials, or model API calls."""
from alpha_research.agents.antigravity import AntigravityModelClient
from google.antigravity import types

client = AntigravityModelClient({
    "provider": "antigravity_sdk",
    "api_key_env": "GEMINI_API_KEY",
    "timeout_seconds": 90,
})
config = client.build_local_config("fixture-only-not-a-real-key")
assert config.capabilities.enable_subagents is False
assert config.capabilities.enabled_tools == [types.BuiltinTools.FINISH]
assert not config.tools
assert not config.mcp_servers
assert not config.skills_paths
assert config.env["GEMINI_API_KEY"] == "fixture-only-not-a-real-key"
assert len(config.policies) >= 2
print("Antigravity SDK config contract: PASS (no provider called)")
