import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nastu_quant.model_client import ConfiguredModelClient, DecisionRecorder, ModelError


class TestConfiguredModelClient(unittest.TestCase):
    def test_anthropic_compatible_base_url_returns_json_object(self):
        requests = []

        def transport(url, headers, payload):
            requests.append((url, headers, payload))
            return {"content": [{"type": "text", "text": '{"dataset_id":"analyst69"}'}],
                    "usage": {"input_tokens": 31, "output_tokens": 9}}

        client = ConfiguredModelClient(
            {"provider": "anthropic_compatible",
             "url": "https://open.bigmodel.cn/api/anthropic",
             "model": "glm-5.3", "api_key_env": "BIGMODEL_API_KEY"},
            transport=transport, environ={"BIGMODEL_API_KEY": "secret"})
        self.assertEqual(client.complete_json("select_dataset", {"region": "GLB"}),
                         {"dataset_id": "analyst69"})
        url, headers, payload = requests[0]
        self.assertEqual(url, "https://open.bigmodel.cn/api/anthropic/v1/messages")
        self.assertEqual(headers["x-api-key"], "secret")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertNotIn("Authorization", headers)
        self.assertEqual(payload["model"], "glm-5.3")
        self.assertGreater(payload["max_tokens"], 0)
        self.assertIn("select_dataset", payload["messages"][0]["content"])
        self.assertEqual(client.last_usage, {"input_tokens": 31, "output_tokens": 9})

    def test_anthropic_compatible_accepts_one_fenced_json_decision(self):
        client = ConfiguredModelClient(
            {"provider": "anthropic_compatible",
             "url": "https://model.example/api/anthropic", "model": "glm-5.3",
             "api_key_env": "TEST_MODEL_KEY"},
            transport=lambda *args: {"content": [{"type": "text", "text":
                'Scope selection\n```json\n{"universe":"TOPDIV3000"}\n```\nValidated.'}]},
            environ={"TEST_MODEL_KEY": "secret"})
        self.assertEqual(client.complete_json("select_scope", {}),
                         {"universe": "TOPDIV3000"})

    def test_anthropic_compatible_requires_key_and_valid_response(self):
        config = {"provider": "anthropic_compatible",
                  "url": "https://open.bigmodel.cn/api/anthropic/v1/messages",
                  "model": "glm-5.3", "api_key_env": "BIGMODEL_API_KEY"}
        client = ConfiguredModelClient(config,
            transport=lambda *args: self.fail("request sent"), environ={})
        with self.assertRaises(ModelError):
            client.complete_json("select_dataset", {})
        client = ConfiguredModelClient(config,
            transport=lambda *args: {"content": [{"type": "thinking", "thinking": "x"}]},
            environ={"BIGMODEL_API_KEY": "secret"})
        with self.assertRaises(ModelError):
            client.complete_json("select_dataset", {})

    def test_invalid_model_text_is_retained_in_local_error_evidence(self):
        client = ConfiguredModelClient(
            {"provider": "anthropic_compatible",
             "url": "https://model.example/api/anthropic", "model": "glm-5.3",
             "api_key_env": "TEST_MODEL_KEY"},
            transport=lambda *args: {"content": [{"type": "text",
                                                   "text": "```json\n{broken}\n```"}]},
            environ={"TEST_MODEL_KEY": "secret"})
        with tempfile.TemporaryDirectory() as root:
            recorder = DecisionRecorder(client, Path(root))
            with self.assertRaises(ModelError):
                recorder.complete_json("select_dataset", {"region": "GLB"})
            records = list((Path(root) / "logs" / "model_calls").glob("*.response.json"))
            self.assertEqual(len(records), 1)
            saved = json.loads(records[0].read_text(encoding="utf-8"))
            self.assertEqual(saved["service_text"], "```json\n{broken}\n```")

    def test_openai_compatible_service_returns_json_object(self):
        requests = []

        def transport(url, headers, payload):
            requests.append((url, headers, payload))
            return {"choices": [{"message": {"content": '{"dataset_id":"risk60"}'}}]}

        client = ConfiguredModelClient(
            {"provider": "openai_compatible", "url": "https://model.example/v1/chat/completions",
             "model": "research-model", "api_key_env": "TEST_MODEL_KEY"},
            transport=transport, environ={"TEST_MODEL_KEY": "secret"})
        self.assertEqual(client.complete_json("select_dataset", {"region": "GLB"}),
                         {"dataset_id": "risk60"})
        url, headers, payload = requests[0]
        self.assertEqual(url, "https://model.example/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer secret")
        self.assertEqual(payload["model"], "research-model")
        self.assertIn("select_dataset", payload["messages"][0]["content"])

    def test_missing_key_fails_before_request(self):
        client = ConfiguredModelClient(
            {"provider": "openai_compatible", "url": "https://model.example/v1/chat/completions",
             "model": "research-model", "api_key_env": "TEST_MODEL_KEY"},
            transport=lambda *args: self.fail("request sent"), environ={})
        with self.assertRaises(ModelError):
            client.complete_json("select_dataset", {})

    def test_json_http_backend_uses_same_agent_contract(self):
        client = ConfiguredModelClient(
            {"provider": "json_http", "url": "https://model.example/research"},
            transport=lambda url, headers, payload: {"result": {"decision": "stop"}},
            environ={})
        self.assertEqual(client.complete_json("next_step", {}), {"decision": "stop"})

    def test_construction_prompt_requests_ex_ante_evidence(self):
        requests = []
        client = ConfiguredModelClient(
            {"provider": "openai_compatible", "url": "https://model.example/v1/chat/completions",
             "model": "research-model", "api_key_env": "TEST_MODEL_KEY"},
            transport=lambda url, headers, payload: (
                requests.append(payload) or {"choices": [{"message": {"content": "{}"}}]}),
            environ={"TEST_MODEL_KEY": "secret"})
        client.complete_json("construct_batch", {})
        prompt = json.dumps(requests[0]["messages"])
        self.assertIn("expected_sign", prompt)
        self.assertIn("failure_criterion", prompt)
        self.assertIn("field constraints", prompt)


if __name__ == "__main__":
    unittest.main()
