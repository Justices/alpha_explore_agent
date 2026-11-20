"""Offline regression tests for CLI/HTTP adapter separation."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_research.integrations.model import ConfiguredModelClient, ModelError
from alpha_research.integrations.model_json import decode_decision
from alpha_research.integrations.model_prompts import KNOWLEDGE_POLICY


class ModelTransportBoundaryTests(unittest.TestCase):
    def test_cli_agent_keeps_tools_disabled_and_never_reaches_http_adapter(self):
        client = ConfiguredModelClient({"provider": "codex_cli"}, environ={})
        result = ('{"classification":"offline"}',
                  {"input_tokens": 5, "output_tokens": 2},
                  "codex-offline", "thread-offline")
        with patch("alpha_research.integrations.model_cli.LocalCliModelTool.invoke",
                   return_value=result) as invoke, \
             patch("alpha_research.integrations.model.complete_http",
                   side_effect=AssertionError("HTTP adapter invoked")):
            self.assertEqual(client.complete_json("attribute_results", {"a": 1}),
                             {"classification": "offline"})
        prompt = json.loads(invoke.call_args.args[0])
        self.assertEqual(prompt["task"], "attribute_results")
        self.assertIn(KNOWLEDGE_POLICY, prompt["instruction"])
        self.assertIn("Do not call tools.", prompt["instruction"])
        self.assertEqual(client.last_model, "codex-offline")
        self.assertEqual(client.last_thread_id, "thread-offline")
        self.assertEqual(client.last_usage["input_tokens"], 5)

    def test_http_api_never_invokes_cli_tools(self):
        requests = []

        def transport(url, headers, payload):
            requests.append((url, headers, payload))
            return {"choices": [{"message": {"content": '{"ok":true}'}}],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 4}}

        client = ConfiguredModelClient(
            {"provider": "openai_compatible", "model": "offline",
             "url": "https://api.example.invalid/v1/chat/completions",
             "api_key_env": "TEST_API_KEY"},
            transport=transport, environ={"TEST_API_KEY": "fixture"},
        )
        with patch("alpha_research.integrations.model.complete_cli",
                   side_effect=AssertionError("CLI invoked")):
            self.assertEqual(client.complete_json("select_dataset", {"region": "CHN"}),
                             {"ok": True})
        self.assertEqual(requests[0][1]["Authorization"], "Bearer fixture")
        self.assertIn(KNOWLEDGE_POLICY,
                      json.loads(requests[0][2]["messages"][0]["content"])["instruction"])
        self.assertEqual(client.last_usage["completion_tokens"], 4)

    def test_http_backend_rejects_cli_research_thread_before_request(self):
        client = ConfiguredModelClient(
            {"provider": "json_http", "url": "https://api.example.invalid"},
            transport=lambda *_: self.fail("unexpected HTTP request"), environ={})
        with self.assertRaisesRegex(ModelError, "cannot use a CLI research thread"):
            client.complete_json("sample", {}, thread_path=Path("thread.json"))

    def test_unknown_provider_fails_closed_without_dispatch(self):
        client = ConfiguredModelClient({"provider": "unsupported_transport"}, environ={})
        with patch("alpha_research.integrations.model.complete_cli") as cli, \
             patch("alpha_research.integrations.model.complete_http") as http:
            with self.assertRaisesRegex(ModelError, "unsupported model provider"):
                client.complete_json("sample", {})
            cli.assert_not_called()
            http.assert_not_called()

    def test_shared_decoder_rejects_multiple_json_blocks(self):
        text = "first ```json\n{\"a\":1}\n``` second ```json\n{\"b\":2}\n```"
        with self.assertRaises(ValueError):
            decode_decision(text)
        self.assertEqual(decode_decision("analysis ```json\n{\"ok\":true}\n```"),
                         {"ok": True})

    def test_http_adapter_resets_prior_usage_between_calls(self):
        calls = 0

        def transport(url, headers, payload):
            nonlocal calls
            calls += 1
            response = {"result": {"call": calls}}
            if calls == 1:
                response["usage"] = {"input_tokens": 4}
            return response

        client = ConfiguredModelClient(
            {"provider": "json_http", "url": "https://api.example.invalid"},
            transport=transport, environ={})
        self.assertEqual(client.complete_json("t", {}), {"call": 1})
        self.assertEqual(client.last_usage, {"input_tokens": 4})
        self.assertEqual(client.complete_json("t", {}), {"call": 2})
        self.assertIsNone(client.last_usage)

    def test_invalid_cli_json_uses_canonical_model_error(self):
        client = ConfiguredModelClient({"provider": "claude_cli"}, environ={})
        with patch("alpha_research.integrations.model_cli.LocalCliModelTool.invoke",
                   return_value=("nonsense", None, None, None)):
            with self.assertRaisesRegex(ModelError, "invalid JSON decision"):
                client.complete_json("sample", {})
            self.assertEqual(client.last_response_text, "nonsense")


if __name__ == "__main__":
    unittest.main()
