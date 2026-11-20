"""Offline separation tests for CLI Agents and OpenAI Python SDK models."""
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from alpha_research.integrations.model import ConfiguredModelClient, ModelError
from alpha_research.integrations.model_json import decode_decision
from alpha_research.integrations.model_prompts import KNOWLEDGE_POLICY


class OfflineSDK:
    def __init__(self, *, responses):
        self.responses = iter(responses)
        self.requests = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def create(self, **kwargs):
        self.requests.append(kwargs)
        text, usage = next(self.responses)
        return SimpleNamespace(
            model="offline-sdk",
            usage=(None if usage is None else SimpleNamespace(**usage)),
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


class ModelTransportBoundaryTests(unittest.TestCase):
    def sdk(self, responses, *, environ=None):
        sdk = OfflineSDK(responses=responses)
        options = []
        def factory(**kwargs):
            options.append(kwargs)
            return sdk
        client = ConfiguredModelClient(
            {"provider": "openai_sdk", "model": "offline",
             "base_url": "https://api.example.invalid/v1",
             "api_key_env": "TEST_API_KEY"},
            sdk_factory=factory,
            environ={"TEST_API_KEY": "fixture"} if environ is None else environ)
        return client, sdk, options

    def test_cli_agent_keeps_tools_disabled_and_never_reaches_sdk(self):
        client = ConfiguredModelClient({"provider": "codex_cli"}, environ={})
        result = ('{"classification":"offline"}',
                  {"input_tokens": 5, "output_tokens": 2},
                  "codex-offline", "thread-offline")
        with patch("alpha_research.integrations.model_cli.LocalCliModelTool.invoke",
                   return_value=result) as invoke, patch(
                       "alpha_research.integrations.model.complete_openai_sdk",
                       side_effect=AssertionError("OpenAI SDK adapter invoked")):
            self.assertEqual(client.complete_json("attribute_results", {"a": 1}),
                             {"classification": "offline"})
        prompt = json.loads(invoke.call_args.args[0])
        self.assertEqual(prompt["task"], "attribute_results")
        self.assertIn(KNOWLEDGE_POLICY, prompt["instruction"])
        self.assertIn("Do not call tools.", prompt["instruction"])
        self.assertEqual(client.last_model, "codex-offline")
        self.assertEqual(client.last_thread_id, "thread-offline")
        self.assertEqual(client.last_usage["input_tokens"], 5)

    def test_sdk_api_never_invokes_cli_tools(self):
        client, sdk, options = self.sdk([
            ('{"ok":true}', {"prompt_tokens": 8, "completion_tokens": 4})])
        with patch("alpha_research.integrations.model_cli.LocalCliModelTool.invoke",
                   side_effect=AssertionError("CLI invoked")):
            self.assertEqual(client.complete_json("select_dataset", {"region": "CHN"}),
                             {"ok": True})
        self.assertEqual(options[0]["api_key"], "fixture")
        self.assertEqual(options[0]["max_retries"], 0)
        self.assertIn(KNOWLEDGE_POLICY,
                      json.loads(sdk.requests[0]["messages"][0]["content"])["instruction"])
        self.assertNotIn("tools", sdk.requests[0])
        self.assertEqual(client.last_usage["completion_tokens"], 4)

    def test_sdk_backend_rejects_cli_research_thread_before_request(self):
        client, sdk, options = self.sdk([
            ('{"ok":true}', None)])
        with self.assertRaisesRegex(ModelError, "cannot use a CLI research thread"):
            client.complete_json("sample", {}, thread_path=Path("thread.json"))
        self.assertEqual(sdk.requests, [])
        self.assertEqual(options, [])

    def test_unknown_provider_fails_closed_without_dispatch(self):
        client = ConfiguredModelClient({"provider": "unsupported_transport"}, environ={})
        with patch("alpha_research.integrations.model.complete_openai_sdk") as sdk:
            with self.assertRaisesRegex(ModelError, "unsupported model provider"):
                client.complete_json("sample", {})
            sdk.assert_not_called()

    def test_malformed_provider_type_is_a_model_error(self):
        for bad in ([], {}, None, 42):
            with self.subTest(provider=bad):
                client = ConfiguredModelClient({"provider": bad}, environ={})
                with self.assertRaisesRegex(ModelError, "model provider"):
                    client.complete_json("sample", {})

    def test_shared_decoder_rejects_multiple_json_blocks(self):
        fence = chr(96) * 3
        text = ("first " + fence + 'json\n{"a":1}\n' + fence +
                " second " + fence + 'json\n{"b":2}\n' + fence)
        with self.assertRaises(ValueError):
            decode_decision(text)
        self.assertEqual(decode_decision("analysis " + fence +
                                         'json\n{"ok":true}\n' + fence),
                         {"ok": True})

    def test_sdk_adapter_resets_prior_usage_between_calls(self):
        client, sdk, _ = self.sdk([
            ('{"call":1}', {"prompt_tokens": 4, "completion_tokens": 1}),
            ('{"call":2}', None)])
        self.assertEqual(client.complete_json("t", {}), {"call": 1})
        self.assertEqual(client.last_usage,
                         {"prompt_tokens": 4, "completion_tokens": 1})
        self.assertEqual(client.complete_json("t", {}), {"call": 2})
        self.assertIsNone(client.last_usage)
        self.assertEqual(len(sdk.requests), 2)

    def test_invalid_cli_json_uses_canonical_model_error(self):
        client = ConfiguredModelClient({"provider": "claude_cli"}, environ={})
        with patch("alpha_research.integrations.model_cli.LocalCliModelTool.invoke",
                   return_value=("nonsense", None, None, None)):
            with self.assertRaisesRegex(ModelError, "invalid JSON decision"):
                client.complete_json("sample", {})
            self.assertEqual(client.last_response_text, "nonsense")


if __name__ == "__main__":
    unittest.main()
