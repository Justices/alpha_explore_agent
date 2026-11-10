import json
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_research.integrations.model import ConfiguredModelClient, DecisionRecorder, ModelError


class TestConfiguredModelClient(unittest.TestCase):
    def test_codex_thread_is_resumed_by_research_id_across_recorders(self):
        thread_ids = iter(["11111111-1111-4111-8111-111111111111",
                           "22222222-2222-4222-8222-222222222222"])
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            thread_id = command[-2] if "resume" in command else next(thread_ids)
            events = [
                {"type": "thread.started", "thread_id": thread_id},
                {"type": "item.completed", "item": {"type": "agent_message",
                                                    "text": '{"ok":true}'}},
                {"type": "turn.completed", "usage": {"input_tokens": 9}},
            ]
            return CompletedProcess(command, 0, "\n".join(map(json.dumps, events)), "")

        with tempfile.TemporaryDirectory() as root, \
                patch("shutil.which", return_value=sys.executable), \
                patch("subprocess.run", side_effect=run):
            for research_id, task in [("regular:first", "plan_probes"),
                                      ("regular:second", "plan_probes"),
                                      ("regular:first", "construct_batch")]:
                recorder = DecisionRecorder(
                    ConfiguredModelClient({"provider": "codex_cli"}), Path(root))
                recorder.begin_session(research_id=research_id)
                self.assertEqual(recorder.complete_json(task, {}), {"ok": True})
            self.assertNotIn("--ephemeral", commands[0])
            self.assertNotIn("resume", commands[1])
            self.assertEqual(commands[2][-2:], ["11111111-1111-4111-8111-111111111111", "-"])
            rows = [json.loads(path.read_text(encoding="utf-8")) for path in
                    (Path(root) / "logs" / "model_calls").glob("*.record.json")]
            first = [row for row in rows if row["research_id"] == "regular:first"]
            self.assertEqual({row["codex_thread_id"] for row in first},
                             {"11111111-1111-4111-8111-111111111111"})

    def test_unknown_codex_thread_creation_blocks_a_second_start(self):
        with tempfile.TemporaryDirectory() as root, \
                patch("shutil.which", return_value=sys.executable), \
                patch("subprocess.run", return_value=CompletedProcess([], 1, "", "")) as run:
            for reason in ("exited with code 1", "thread creation is unresolved"):
                recorder = DecisionRecorder(
                    ConfiguredModelClient({"provider": "codex_cli"}), Path(root))
                recorder.begin_session(research_id="regular:ambiguous")
                with self.assertRaisesRegex(ModelError, reason):
                    recorder.complete_json("plan_probes", {})
            self.assertEqual(run.call_count, 1)

    def test_configured_routes_use_agy_and_record_selected_backend(self):
        from alpha_research.integrations.model import build_model_client

        agy = {"event": "result", "result": {"status": "SUCCESS",
               "response": '{"classification":"fixture"}',
               "usage": {"input_tokens": 7, "output_tokens": 2}}}
        model = build_model_client({
            "provider": "router", "default": "http",
            "routes": {"attribute_results": "agy", "diagnose_failures": "agy"},
            "backends": {"agy": {"provider": "agy_cli", "executable": "agy"},
                         "http": {"provider": "json_http", "url": "https://model.example"}},
        })
        with tempfile.TemporaryDirectory() as root, \
                patch("shutil.which", return_value=sys.executable), \
                patch("subprocess.run", return_value=CompletedProcess(
                    [], 0, json.dumps(agy), "")) as run:
            recorder = DecisionRecorder(model, Path(root))
            for task in ("attribute_results", "diagnose_failures"):
                self.assertEqual(recorder.complete_json(task, {}),
                                 {"classification": "fixture"})
            self.assertEqual(run.call_count, 2)
            command = run.call_args.args[0]
            self.assertIn("--input-format=stream-json", command)
            self.assertNotIn("diagnose_failures", command)
            prompt = json.loads(run.call_args.kwargs["input"])
            self.assertIn("diagnose_failures", prompt["message"]["content"])
            rows = [json.loads(path.read_text(encoding="utf-8")) for path in
                    (Path(root) / "logs" / "model_calls").glob("*.record.json")]
            self.assertEqual({row["provider"] for row in rows}, {"agy_cli"})
            self.assertEqual({row["token_in"] for row in rows}, {7})
            self.assertEqual(model.config_for("construct_batch")["provider"], "json_http")

    def test_codex_and_claude_tools_parse_json_and_usage(self):
        fixtures = {
            "codex_cli": "\n".join(json.dumps(event) for event in [
                {"type": "item.completed", "item": {"type": "agent_message",
                                                    "text": '{"ok":true}'}},
                {"type": "turn.completed", "usage": {"input_tokens": 9}}]),
            "claude_cli": json.dumps({"type": "result", "subtype": "success",
                                      "result": '{"ok":true}', "usage": {"input_tokens": 9}}),
        }
        for provider, output in fixtures.items():
            with self.subTest(provider=provider), \
                    patch("shutil.which", return_value=sys.executable), \
                    patch("subprocess.run", return_value=CompletedProcess([], 0, output, "")) as run:
                client = ConfiguredModelClient(
                    {"provider": provider},
                    environ={"PATH": "fixture", "BRAIN_CREDENTIALS_FILE": "secret"})
                self.assertEqual(client.complete_json("construct_batch", {}), {"ok": True})
                self.assertEqual(client.last_usage, {"input_tokens": 9})
                self.assertNotIn("BRAIN_CREDENTIALS_FILE", run.call_args.kwargs["env"])
                self.assertNotIn("construct_batch", run.call_args.args[0])

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
