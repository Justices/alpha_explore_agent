"""Offline tests for canonical model prompts and fail-closed routing input."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from alpha_research.agents.antigravity import AntigravityModelClient
from alpha_research.agents.configuration import (
    AgentConfigError, RoleRoutingConfig, backend_kind,
)
from alpha_research.integrations.model import ConfiguredModelClient
from alpha_research.integrations.model_prompts import (
    KNOWLEDGE_POLICY, TASK_INSTRUCTIONS, instruction_for,
)
from alpha_research.agents.registry import bundled_profile_path


def minimal_config():
    return {
        "provider": "role_router",
        "default_role": "research_lead",
        "roles": {"research_lead": {"backend": "test_api"}},
        "task_roles": {"plan_experiments": "research_lead"},
        "backends": {
            "test_api": {"provider": "openai_sdk",
                         "base_url": "https://api.example.invalid/v1",
                         "model": "offline-model", "api_key_env": "OFFLINE_KEY"}
        },
    }


class PromptBoundaryTests(unittest.TestCase):
    def test_every_canonical_task_has_an_instruction(self):
        profile = json.loads(bundled_profile_path().read_text(encoding="utf-8"))
        self.assertFalse(set(profile["task_roles"]) - set(TASK_INSTRUCTIONS))
        self.assertIn("model_probe", TASK_INSTRUCTIONS)

    def test_task_and_knowledge_rules_are_single_sourced(self):
        prompt = instruction_for("construct_batch")
        self.assertIn("State the economic mechanism", prompt)
        self.assertIn(KNOWLEDGE_POLICY, prompt)
        self.assertEqual(prompt.count(KNOWLEDGE_POLICY), 1)
        self.assertEqual(instruction_for("unknown_task").split(" ")[0], "Return")
        self.assertTrue(instruction_for("construct_batch", forbid_tools=True)
                        .endswith("Answer from the supplied evidence. Do not call tools."))

    def test_api_transport_uses_shared_task_instructions(self):
        requests = []

        class SDK:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def __init__(self):
                self.chat = SimpleNamespace(completions=SimpleNamespace(
                    create=self.create))
            def create(self, **kwargs):
                requests.append(kwargs)
                return SimpleNamespace(
                    model="offline-model", usage=None,
                    choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                        content='{"ok":true}'))])

        client = ConfiguredModelClient(
            {"provider": "openai_sdk",
             "base_url": "https://api.example.invalid/v1",
             "model": "offline-model", "api_key_env": "OFFLINE_KEY"},
            sdk_factory=lambda **kw: SDK(),
            environ={"OFFLINE_KEY": "fixture"},
        )
        self.assertEqual(client.complete_json("plan_experiments", {"round": 1}),
                         {"ok": True})
        self.assertEqual(
            json.loads(requests[0]["messages"][0]["content"])["instruction"],
            instruction_for("plan_experiments"))

    def test_agy_transport_uses_same_task_rules_without_real_sdk(self):
        client = AntigravityModelClient(
            {"provider": "antigravity_sdk", "api_key_env": "AGY_TEST_KEY"},
            environ={"AGY_TEST_KEY": "offline-fixture"},
        )
        with patch.object(client, "_run", new_callable=AsyncMock) as run:
            run.return_value = '{"ok": true}'
            self.assertEqual(client.complete_json("attribute_results", {"test": True}),
                             {"ok": True})
            sent = json.loads(run.call_args.args[0])
        self.assertEqual(sent["instruction"],
                         instruction_for("attribute_results", forbid_tools=True))
        self.assertEqual(sent["context"], {"test": True})


class RoutingValidationTests(unittest.TestCase):
    def test_parsed_config_is_immune_to_input_mutation(self):
        source = minimal_config()
        parsed = RoleRoutingConfig.parse(source)
        source["roles"]["research_lead"]["backend"] = "unknown"
        source["backends"]["test_api"]["base_url"] = "http://not-safe"
        source["task_roles"]["plan_experiments"] = "unknown"
        selected = parsed.resolve("plan_experiments")
        self.assertEqual(selected.backend_name, "test_api")
        self.assertEqual(selected.backend["base_url"], "https://api.example.invalid/v1")

    def test_malformed_nested_types_fail_as_config_errors(self):
        for item, replacement in (
            ("provider", []),
            ("provider", {}),
            ("executable", []),
            ("response_format", []),
        ):
            with self.subTest(field=item):
                source = minimal_config()
                if item == "executable":
                    source["backends"]["test_api"] = {
                        "provider": "codex_cli", "executable": replacement
                    }
                elif item == "response_format":
                    source["backends"]["test_api"] = {
                        "provider": "openai_sdk", "model": "test",
                        "base_url": "https://api.example.invalid/v1",
                        "api_key_env": "API_TEST_KEY",
                        "response_format": replacement,
                    }
                else:
                    source["backends"]["test_api"][item] = replacement
                with self.assertRaises(AgentConfigError):
                    RoleRoutingConfig.parse(source)

        for location in ("role_backend", "task_role"):
            with self.subTest(location=location):
                source = minimal_config()
                if location == "role_backend":
                    source["roles"]["research_lead"]["backend"] = []
                else:
                    source["task_roles"]["plan_experiments"] = []
                with self.assertRaises(AgentConfigError):
                    RoleRoutingConfig.parse(source)

        with self.assertRaises(AgentConfigError):
            backend_kind([])

    def test_malformed_https_endpoints_are_rejected(self):
        for url in (
            "https://",
            "https://user:password@example.invalid/v1",
            "https://[malformed",
            "https://api.example.invalid/v1#ignored",
        ):
            with self.subTest(url=url):
                source = minimal_config()
                source["backends"]["test_api"]["base_url"] = url
                with self.assertRaisesRegex(AgentConfigError, "HTTPS API base_url"):
                    RoleRoutingConfig.parse(source)

    def test_cli_executable_and_agy_key_must_not_be_blank(self):
        for provider, bad_spec in (
            ("codex_cli", {"provider": "codex_cli", "executable": "  "}),
            ("claude_cli", {"provider": "claude_cli", "executable": 10}),
            ("antigravity_sdk", {"provider": "antigravity_sdk", "api_key_env": " "}),
        ):
            with self.subTest(provider=provider):
                source = minimal_config()
                source["backends"]["test_api"] = copy.deepcopy(bad_spec)
                with self.assertRaises(AgentConfigError):
                    RoleRoutingConfig.parse(source)


if __name__ == "__main__":
    unittest.main()
