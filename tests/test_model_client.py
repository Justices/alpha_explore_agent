import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nastu_quant.model_client import ConfiguredModelClient, ModelError


class TestConfiguredModelClient(unittest.TestCase):
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
