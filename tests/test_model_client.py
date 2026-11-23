import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_research.integrations.model import ConfiguredModelClient, DecisionRecorder, ModelError


class OfflineOpenAISDK:
    """Offline SDK surface matching OpenAI.chat.completions.create."""
    def __init__(self, content, calls, options, *, usage=None, error=None):
        self.content, self.calls, self.options = content, calls, options
        self.usage, self.error = usage, error
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def create(self, **request):
        self.calls.append((self.options, request))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            model=request["model"],
            usage=SimpleNamespace(
                prompt_tokens=(self.usage or (31, 9))[0],
                completion_tokens=(self.usage or (31, 9))[1]),
            choices=[SimpleNamespace(finish_reason="stop",
                message=SimpleNamespace(content=self.content))])


def offline_sdk(content, calls, *, usage=None, error=None):
    return lambda **opts: OfflineOpenAISDK(
        content, calls, opts, usage=usage, error=error)


class TestConfiguredModelClient(unittest.TestCase):
    def test_claude_invalid_decision_preserves_raw_response(self):
        output = {"type": "result", "subtype": "success",
                  "result": "The simulations did not support the hypothesis."}
        client = ConfiguredModelClient({"provider": "claude_cli"})
        with patch("shutil.which", return_value=sys.executable), \
                patch("subprocess.run", return_value=CompletedProcess(
                    [], 0, json.dumps(output), "")) as run:
            with self.assertRaisesRegex(ModelError, "invalid JSON decision"):
                client.complete_json("attribute_results", {})
        prompt = json.loads(run.call_args.kwargs["input"])
        self.assertIn("exactly one JSON object", prompt["instruction"])
        self.assertEqual(client.last_response_text, output["result"])

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

    def test_role_router_selects_claude_runtime_and_records_identity(self):
        from alpha_research.integrations.model import build_model_client

        output = {"type": "result", "subtype": "success",
                  "result": '{"classification":"fixture"}',
                  "usage": {"input_tokens": 7, "output_tokens": 2}}
        model = build_model_client({
            "provider": "role_router", "default_role": "research_lead",
            "task_roles": {"attribute_results": "research_executor",
                           "diagnose_failures": "research_executor"},
            "roles": {"research_lead": {"backend": "http"},
                      "research_executor": {"backend": "claude"}},
            "backends": {
                "claude": {"provider": "claude_cli", "session": "ephemeral"},
                "http": {"provider": "openai_sdk", "base_url": "https://model.example/v1",
                         "model": "research-model", "api_key_env": "TEST_MODEL_KEY"},
            },
        })
        with tempfile.TemporaryDirectory() as root, \
                patch("shutil.which", return_value=sys.executable), \
                patch("subprocess.run", return_value=CompletedProcess(
                    [], 0, json.dumps(output), "")) as run:
            recorder = DecisionRecorder(model, Path(root))
            for task in ("attribute_results", "diagnose_failures"):
                self.assertEqual(recorder.complete_json(task, {}),
                                 {"classification": "fixture"})
            self.assertEqual(run.call_count, 2)
            command = run.call_args.args[0]
            self.assertIn("--no-session-persistence", command)
            self.assertIn("--tools", command)
            prompt = json.loads(run.call_args.kwargs["input"])
            self.assertEqual(prompt["task"], "diagnose_failures")
            rows = [json.loads(path.read_text(encoding="utf-8")) for path in
                    (Path(root) / "logs" / "model_calls").glob("*.record.json")]
            self.assertEqual({row["provider"] for row in rows}, {"claude_cli"})
            self.assertEqual({row["agent_role"] for row in rows}, {"research_executor"})
            self.assertEqual({row["token_in"] for row in rows}, {7})
            self.assertEqual(model.config_for("construct_batch")["provider"], "openai_sdk")

    def test_old_router_and_agy_cli_are_not_runtime_entrypoints(self):
        from alpha_research.integrations.model import build_model_client
        with self.assertRaisesRegex(ModelError, "provider=role_router"):
            build_model_client({"provider": "router", "backends": {}})
        with self.assertRaisesRegex(ModelError, "provider=role_router"):
            build_model_client({"provider": "agy_cli"})

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

    def test_openai_sdk_model_api_uses_base_url_and_no_retries(self):
        requests = []
        client = ConfiguredModelClient(
            {"provider": "openai_sdk", "base_url": "https://model.example/v1",
             "model": "research-model", "api_key_env": "TEST_MODEL_KEY"},
            sdk_factory=offline_sdk('{"dataset_id":"risk60"}', requests),
            environ={"TEST_MODEL_KEY": "secret"})
        self.assertEqual(client.complete_json("select_dataset", {"region": "GLB"}),
                         {"dataset_id": "risk60"})
        opts, request = requests[0]
        self.assertEqual(opts["api_key"], "secret")
        self.assertEqual(opts["base_url"], "https://model.example/v1")
        self.assertEqual(opts["max_retries"], 0)
        self.assertEqual(request["model"], "research-model")
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertIn("select_dataset", request["messages"][0]["content"])
        self.assertNotIn("tools", request)
        self.assertEqual(client.last_usage,
                         {"prompt_tokens": 31, "completion_tokens": 9})
        self.assertEqual(client.last_model, "research-model")

    def test_openai_sdk_supports_fenced_json_and_none_response_format(self):
        requests = []
        client = ConfiguredModelClient(
            {"provider": "openai_sdk", "base_url": "https://model.example/v1",
             "model": "research-model", "api_key_env": "TEST_MODEL_KEY",
             "response_format": "none"},
            sdk_factory=offline_sdk(
                'Scope selection\n```json\n{"universe":"TOPDIV3000"}\n```\nValidated.',
                requests), environ={"TEST_MODEL_KEY": "secret"})
        self.assertEqual(client.complete_json("select_scope", {}),
                         {"universe": "TOPDIV3000"})
        self.assertNotIn("response_format", requests[0][1])

    def test_openai_sdk_missing_key_fails_without_sdk_creation(self):
        client = ConfiguredModelClient(
            {"provider": "openai_sdk", "base_url": "https://model.example/v1",
             "model": "research-model", "api_key_env": "TEST_MODEL_KEY"},
            sdk_factory=lambda **opts: self.fail("SDK opened without a key"),
            environ={})
        with self.assertRaises(ModelError):
            client.complete_json("select_dataset", {})

    def test_openai_sdk_invalid_text_preserved_in_durable_error_evidence(self):
        requests = []
        client = ConfiguredModelClient(
            {"provider": "openai_sdk", "base_url": "https://model.example/v1",
             "model": "research-model", "api_key_env": "TEST_MODEL_KEY"},
            sdk_factory=offline_sdk("```json\\n{broken}\\n```", requests),
            environ={"TEST_MODEL_KEY": "secret"})
        with tempfile.TemporaryDirectory() as root:
            recorder = DecisionRecorder(client, Path(root))
            with self.assertRaisesRegex(ModelError, "valid JSON"):
                recorder.complete_json("select_dataset", {"region": "GLB"})
            files = list((Path(root) / "logs" / "model_calls").glob("*.response.json"))
            self.assertEqual(len(files), 1)
            stored = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(stored["service_text"], "```json\\n{broken}\\n```")
            self.assertEqual(stored["status"], "error")

    def test_old_raw_http_providers_are_rejected(self):
        for provider in ("openai_compatible", "anthropic_compatible", "json_http"):
            with self.subTest(provider=provider):
                client = ConfiguredModelClient(
                    {"provider": provider},
                    sdk_factory=lambda **opts: self.fail("legacy route reached"),
                    environ={"TEST_MODEL_KEY": "fixture"})
                with self.assertRaisesRegex(ModelError, "unsupported model provider"):
                    client.complete_json("select_dataset", {})

    def test_construction_prompt_requests_ex_ante_evidence(self):
        calls = []
        client = ConfiguredModelClient(
            {"provider": "openai_sdk", "base_url": "https://model.example/v1",
             "model": "research-model", "api_key_env": "TEST_MODEL_KEY"},
            sdk_factory=offline_sdk("{}", calls),
            environ={"TEST_MODEL_KEY": "secret"})
        client.complete_json("construct_batch", {})
        prompt = json.dumps(calls[0][1]["messages"])
        self.assertIn("expected_sign", prompt)
        self.assertIn("failure_criterion", prompt)
        self.assertIn("field constraints", prompt)

    def test_batch_audit_is_incremental_and_rebinding_does_not_recopy(self):
        from alpha_research.integrations.model import build_model_client
        with tempfile.TemporaryDirectory() as root:
            config = json.loads((Path(__file__).resolve().parent.parent /
                                 "config.mock.example.json").read_text(encoding="utf-8"))["model"]
            recorder = DecisionRecorder(build_model_client(config), Path(root))
            recorder.begin_session(research_id="R1")
            batch = Path(root) / "batch"
            recorder.bind_batch(batch)
            recorder.complete_json("plan_experiments", {"round_index": 0})
            first_manifest = json.loads(
                (batch / "model_call_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(first_manifest["calls"]), 1)
            self.assertTrue(first_manifest["calls"][0]["response_sha256"])
            with patch("alpha_research.integrations.model_journal.shutil.copy2",
                       side_effect=AssertionError("old model audit copied twice")):
                recorder.bind_batch(batch)
            recorder.complete_json("attribute_results", {"round_index": 0})
            manifest = json.loads(
                (batch / "model_call_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["calls"]), 2)
            events = [json.loads(line) for line in (
                Path(root) / "logs" / "model_log.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(events), 4)
            self.assertEqual({row["status"] for row in events},
                             {"in_flight", "ok"})
            self.assertEqual(len({row["call_id"] for row in events}), 2)

    def test_corrupted_model_manifest_blocks_reexecution(self):
        from alpha_research.integrations.model import build_model_client
        with tempfile.TemporaryDirectory() as root:
            config = json.loads((Path(__file__).resolve().parent.parent /
                                 "config.mock.example.json").read_text(encoding="utf-8"))["model"]
            batch = Path(root) / "batch"
            first = DecisionRecorder(build_model_client(config), Path(root))
            first.begin_session(research_id="R1")
            first.bind_batch(batch)
            first.complete_json("attribute_results", {})
            manifest = batch / "model_call_manifest.json"
            manifest.write_text("{truncated", encoding="utf-8")
            second = DecisionRecorder(build_model_client(config), Path(root))
            second.begin_session(research_id="R1")
            second.batch_dir = batch
            with self.assertRaisesRegex(ModelError, "unreadable; replay blocked"):
                second.complete_json("attribute_results", {})
            self.assertEqual(len(list((Path(root) / "logs" / "model_calls")
                                      .glob("*.record.json"))), 1)

    def test_missing_previous_model_response_blocks_automatic_retry(self):
        from alpha_research.integrations.model import build_model_client
        with tempfile.TemporaryDirectory() as root:
            config = json.loads((Path(__file__).resolve().parent.parent /
                                 "config.mock.example.json").read_text(encoding="utf-8"))["model"]
            batch = Path(root) / "batch"
            first = DecisionRecorder(build_model_client(config), Path(root))
            first.begin_session(research_id="R1")
            first.bind_batch(batch)
            first.complete_json("attribute_results", {})
            manifest = json.loads((batch / "model_call_manifest.json")
                                  .read_text(encoding="utf-8"))["calls"][0]
            Path(manifest["response_ref"]).unlink()
            Path(manifest["global_response_ref"]).unlink()
            second = DecisionRecorder(build_model_client(config), Path(root))
            second.begin_session(research_id="R1")
            second.bind_batch(batch)
            with self.assertRaisesRegex(ModelError, "retry blocked"):
                second.complete_json("attribute_results", {})
            self.assertEqual(len(list((Path(root) / "logs" / "model_calls")
                                      .glob("*.record.json"))), 1)


    def test_real_provider_timeout_does_not_implicitly_retry_on_recovery(self):
        with tempfile.TemporaryDirectory() as root:
            batch = Path(root) / "batch"
            calls = []
            provider = ConfiguredModelClient({
                "provider": "openai_sdk",
                "base_url": "https://model.example/v1",
                "model": "remote-model", "api_key_env": "RESEARCH_KEY",
            }, sdk_factory=offline_sdk(
                "{}", calls,
                error=TimeoutError("response lost after provider POST")),
                environ={"RESEARCH_KEY": "fixture"})
            before = DecisionRecorder(provider, Path(root))
            before.begin_session(research_id="remote:R1")
            before.bind_batch(batch)
            with self.assertRaisesRegex(ModelError, "SDK model request failed"):
                before.complete_json("attribute_results", {"batch": 1})
            resumed = DecisionRecorder(provider, Path(root))
            resumed.begin_session(research_id="remote:R1")
            resumed.bind_batch(batch)
            with self.assertRaisesRegex(ModelError, "explicit state reconciliation"):
                resumed.complete_json("attribute_results", {"batch": 1})
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0]["max_retries"], 0)


if __name__ == "__main__":
    unittest.main()
