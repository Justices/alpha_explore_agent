"""Offline contract tests; no provider SDK or BRAIN service is called."""
import json
import sys
import tempfile
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch
from pathlib import Path

from alpha_research.agents.antigravity import (
    AntigravityAdapterError, AntigravityModelClient, decode_json_object
)
from alpha_research.agents.configuration import AgentConfigError
from alpha_research.integrations.model import build_model_client, DecisionRecorder, ModelError, ConfiguredModelClient
from alpha_research.agents.router import RoleRoutedModelClient
from alpha_research.research_cycle import OfflineResearchCycle, OfflineSimulationGateway
from alpha_research.research_cycle.contracts import ResearchContractError

ROOT = Path(__file__).resolve().parent.parent


def config():
    return json.loads((ROOT / "config.mock.example.json").read_text(encoding="utf-8"))["model"]


def gateway():
    fixtures = ROOT / "tests" / "fixtures" / "research_cycle_simulations.json"
    return OfflineSimulationGateway(json.loads(fixtures.read_text(encoding="utf-8")))

def api_role_config():
    """Build protocol fixtures from the canonical role template, no old config file."""
    cfg = config()
    cfg["roles"]["research_lead"]["backend"] = "research_lead_api"
    cfg["roles"]["research_executor"]["backend"] = "research_executor_api"
    cfg["backends"] = {
        "research_lead_api": {
            "provider": "openai_compatible",
            "url": "https://lead-provider.example.invalid/v1/chat/completions",
            "model": "<YOUR_LEAD_MODEL_ID>",
            "api_key_env": "RESEARCH_LEAD_API_KEY",
            "response_format": "none",
        },
        "research_executor_api": {
            "provider": "anthropic_compatible",
            "url": "https://executor-provider.example.invalid/v1/messages",
            "model": "<YOUR_EXECUTOR_MODEL_ID>",
            "api_key_env": "RESEARCH_EXECUTOR_API_KEY",
        },
    }
    return cfg





class TestConfigurableAgents(unittest.TestCase):
    def test_role_inspection_is_offline_and_credential_safe(self):
        from alpha_research.agents.inspect import inspect_roles
        cfg = config()
        cfg["backends"]["agy_mock"] = {
            "provider": "antigravity_sdk",
            "api_key_env": "GEMINI_API_KEY",
            "timeout_seconds": 90,
        }
        report = inspect_roles(cfg, ["plan_experiments", "prepare_execution"])
        self.assertTrue(report["validated"])
        self.assertFalse(report["provider_invoked"])
        self.assertEqual(report["task_routes"]["prepare_execution"]["role"],
                         "research_executor")
        self.assertEqual(report["roles"]["research_executor"]["api_key_env"],
                         "GEMINI_API_KEY")
        self.assertEqual(report["roles"]["research_lead"]["provider"], "mock")
        self.assertNotIn("responses", json.dumps(report))

    def test_role_inspection_rejects_unsafe_configuration_without_loading_sdk(self):
        from alpha_research.agents.inspect import inspect_roles
        cfg = config()
        cfg["backends"]["agy_mock"] = {
            "provider": "antigravity_sdk", "api_key_env": "GEMINI_API_KEY",
            "mcp_servers": ["unapproved"],
        }
        with self.assertRaises(AgentConfigError):
            inspect_roles(cfg, ["attribute_results"])

    def test_roles_can_use_different_http_model_families_without_code_change(self):
        cfg = api_role_config()
        traces = []
        credentials = {
            "RESEARCH_LEAD_API_KEY": "offline-lead-key",
            "RESEARCH_EXECUTOR_API_KEY": "offline-executor-key",
        }

        def fake_transport(url, headers, payload):
            traces.append((url, dict(headers), dict(payload)))
            if "lead-provider" in url:
                self.assertEqual(payload["model"], "<YOUR_LEAD_MODEL_ID>")
                self.assertEqual(headers["Authorization"], "Bearer offline-lead-key")
                self.assertNotIn("response_format", payload)
                return {"choices": [{"message": {"content": '{"plan":true}'}}]}
            self.assertEqual(payload["model"], "<YOUR_EXECUTOR_MODEL_ID>")
            self.assertEqual(headers["x-api-key"], "offline-executor-key")
            self.assertEqual(headers["anthropic-version"], "2023-06-01")
            return {"content": [{"type": "text", "text": '{"attributed":true}'}]}

        router = RoleRoutedModelClient(
            cfg,
            backend_factory=lambda backend: ConfiguredModelClient(
                backend, transport=fake_transport, environ=credentials),
        )
        self.assertEqual(router.role_for("plan_experiments"), "research_lead")
        self.assertEqual(router.backend_name_for("plan_experiments"), "research_lead_api")
        self.assertEqual(router.complete_json("plan_experiments", {"a": 1}),
                         {"plan": True})
        self.assertEqual(router.role_for("attribute_results"), "research_executor")
        self.assertEqual(router.backend_name_for("attribute_results"),
                         "research_executor_api")
        self.assertEqual(router.complete_json("attribute_results", {"a": 2}),
                         {"attributed": True})
        self.assertEqual(len(traces), 2)

        # Exchange models purely through configuration; no task/business code changes.
        swapped = api_role_config()
        swapped["roles"]["research_lead"]["backend"] = "research_executor_api"
        swapped["roles"]["research_executor"]["backend"] = "research_lead_api"
        reversed_router = RoleRoutedModelClient(
            swapped,
            backend_factory=lambda backend: ConfiguredModelClient(
                backend, transport=fake_transport, environ=credentials),
        )
        self.assertEqual(reversed_router.complete_json("plan_experiments", {}),
                         {"attributed": True})
        self.assertEqual(reversed_router.complete_json("attribute_results", {}),
                         {"plan": True})
        self.assertEqual(len(traces), 4)

    def test_http_model_configurations_fail_closed(self):
        cfg = api_role_config()
        cfg["backends"]["research_lead_api"]["url"] = "http://public-server/"
        with self.assertRaises(AgentConfigError):
            build_model_client(cfg)
        cfg["backends"]["research_lead_api"]["url"] = "https://example.invalid/v1/chat/completions"
        cfg["backends"]["research_lead_api"]["api_key_env"] = ""
        with self.assertRaises(AgentConfigError):
            build_model_client(cfg)
        cfg["backends"]["research_lead_api"]["api_key_env"] = "RESEARCH_LEAD_API_KEY"
        cfg["backends"]["research_lead_api"]["response_format"] = "unsupported"
        with self.assertRaises(AgentConfigError):
            build_model_client(cfg)
        cfg["backends"]["research_lead_api"]["response_format"] = "none"
        cfg["backends"]["research_lead_api"]["additional_permissions"] = ["file_write"]
        with self.assertRaises(AgentConfigError):
            build_model_client(cfg)

    def test_http_timeout_setting_reaches_default_transport(self):
        from alpha_research.integrations import model_http as model_http_module
        cfg = {
            "provider": "openai_compatible",
            "url": "https://api.example.invalid/v1/chat/completions",
            "model": "test-model",
            "api_key_env": "TEST_API_KEY",
            "timeout_seconds": 19,
        }
        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, exc_type, exc, tb):
                return False
            def read(self, *args):
                return b'{"choices":[{"message":{"content":"{\\"ok\\": true}"}}]}'
        seen = []
        def fake_urlopen(request, *, timeout):
            seen.append(timeout)
            return FakeResponse()
        # json.load works with a file-like .read() response.
        with patch.object(model_http_module, "urlopen", side_effect=fake_urlopen):
            result = ConfiguredModelClient(cfg, environ={"TEST_API_KEY": "fixture"}).complete_json(
                "plan_experiments", {"fixture": 1})
        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen, [19])

    def test_routes_lead_and_executor_by_role(self):
        m = build_model_client(config())
        self.assertEqual(m.role_for("plan_experiments"), "research_lead")
        self.assertEqual(m.role_for("attribute_results"), "research_executor")
        self.assertEqual(m.complete_json("attribute_results", {})["summary"], "Synthetic experiment only")

    def test_replace_backend_by_config_without_changing_workflow(self):
        cfg = config()
        cfg["backends"]["second_executor"] = {
            "provider": "mock",
            "responses": {"attribute_results": {"summary": "replacement", "findings": []}}
        }
        cfg["roles"]["research_executor"]["backend"] = "second_executor"
        self.assertEqual(build_model_client(cfg).complete_json("attribute_results", {})["summary"],
                         "replacement")

    def test_reject_legacy_agy_cli_in_unattended_router(self):
        cfg = config()
        cfg["backends"]["agy_mock"] = {"provider": "agy_cli"}
        with self.assertRaises(AgentConfigError):
            build_model_client(cfg)

    def test_reject_unknown_backend(self):
        cfg = config()
        cfg["roles"]["research_executor"]["backend"] = "missing"
        with self.assertRaises(AgentConfigError):
            build_model_client(cfg)

    def test_reject_agy_tools_in_p0(self):
        cfg = config()
        cfg["backends"]["agy_mock"] = {
            "provider": "antigravity_sdk", "api_key_env": "GEMINI_API_KEY",
            "tools": ["run_simulation"]
        }
        with self.assertRaises(AgentConfigError):
            build_model_client(cfg)

    def test_agy_missing_key_stops_before_network(self):
        client = AntigravityModelClient(
            {"provider": "antigravity_sdk", "api_key_env": "MISSING_TEST_KEY"}, environ={}
        )
        with self.assertRaises(AntigravityAdapterError):
            client.complete_json("attribute_results", {})

    def test_strict_agy_json_response(self):
        self.assertEqual(decode_json_object('{"ok":true}'), {"ok": True})
        fenced = "Text:\n" + chr(96) * 3 + 'json\n{"a":1}\n' + chr(96) * 3
        self.assertEqual(decode_json_object(fenced), {"a": 1})
        with self.assertRaises(AntigravityAdapterError):
            decode_json_object("not a JSON object")

    def test_recorder_preserves_selected_role(self):
        with tempfile.TemporaryDirectory() as td:
            recorder = DecisionRecorder(build_model_client(config()), Path(td))
            recorder.begin_session(research_id="R1")
            recorder.complete_json("attribute_results", {"evidence": "synthetic"})
            records = list((Path(td) / "logs" / "model_calls").glob("*.record.json"))
            self.assertEqual(len(records), 1)
            row = json.loads(records[0].read_text(encoding="utf-8"))
            self.assertEqual(row["agent_role"], "research_executor")
            self.assertEqual(row["provider"], "mock")

    def test_cli_config_rejects_silent_privilege_expansion(self):
        cfg = config()
        cfg["backends"]["codex_mock"] = {
            "provider": "codex_cli", "sandbox": "workspace-write"
        }
        with self.assertRaises(AgentConfigError):
            build_model_client(cfg)
        cfg["backends"]["codex_mock"]["sandbox"] = "read-only"
        cfg["backends"]["codex_mock"]["session"] = "unsupported"
        with self.assertRaises(AgentConfigError):
            build_model_client(cfg)
        cfg["backends"]["codex_mock"]["session"] = "ephemeral"
        cfg["backends"]["codex_mock"]["extra_args"] = ["dangerous"]
        with self.assertRaises(AgentConfigError):
            build_model_client(cfg)

    def test_unsafe_agy_skills_and_inline_secrets_are_rejected(self):
        cfg = config()
        cfg["backends"]["agy_mock"] = {
            "provider": "antigravity_sdk", "api_key_env": "GEMINI_API_KEY",
            "skills_paths": ["/external/skills"],
        }
        with self.assertRaises(AgentConfigError):
            build_model_client(cfg)
        cfg = config()
        cfg["backends"]["codex_mock"]["api_key"] = "plaintext-secret"
        with self.assertRaises(AgentConfigError):
            build_model_client(cfg)

    def test_codex_ephemeral_session_does_not_create_thread_binding(self):
        cfg = config()
        cfg["backends"]["codex_mock"] = {
            "provider": "codex_cli", "sandbox": "read-only",
            "session": "ephemeral",
        }
        stdout = "\n".join(json.dumps(x) for x in [
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": '{"ok":true}'}},
            {"type": "turn.completed", "usage": {"input_tokens": 1}},
        ])
        with tempfile.TemporaryDirectory() as root, \
             patch("shutil.which", return_value=sys.executable), \
             patch("subprocess.run", return_value=CompletedProcess([], 0, stdout, "")) as run:
            recorder = DecisionRecorder(build_model_client(cfg), Path(root))
            recorder.begin_session(research_id="R1")
            self.assertEqual(recorder.complete_json("plan_experiments", {}), {"ok": True})
            self.assertIn("--ephemeral", run.call_args.args[0])
            self.assertFalse((Path(root) / "codex_threads").exists())

    def test_codex_research_roles_have_isolated_threads(self):
        cfg = config()
        cfg["backends"]["codex_mock"] = {
            "provider": "codex_cli", "sandbox": "read-only",
            "session": "research",
        }
        cfg["roles"]["research_executor"]["backend"] = "codex_mock"
        threads = iter([
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002",
        ])
        commands = []
        def fake_run(command, **kwargs):
            commands.append(command)
            events = [
                {"type": "thread.started", "thread_id": next(threads)},
                {"type": "item.completed", "item": {
                    "type": "agent_message", "text": '{"ok":true}'}},
                {"type": "turn.completed", "usage": {"input_tokens": 1}},
            ]
            return CompletedProcess(command, 0, "\n".join(map(json.dumps, events)), "")
        with tempfile.TemporaryDirectory() as root, \
             patch("shutil.which", return_value=sys.executable), \
             patch("subprocess.run", side_effect=fake_run):
            recorder = DecisionRecorder(build_model_client(cfg), Path(root))
            recorder.begin_session(research_id="R1")
            self.assertEqual(recorder.complete_json("plan_experiments", {}), {"ok": True})
            self.assertEqual(recorder.complete_json("attribute_results", {}), {"ok": True})
            bindings = list((Path(root) / "codex_threads").glob("*/thread.json"))
            self.assertEqual(len(bindings), 2)
            self.assertEqual(
                {json.loads(p.read_text(encoding="utf-8"))["thread_id"] for p in bindings},
                {"00000000-0000-4000-8000-000000000001",
                 "00000000-0000-4000-8000-000000000002"})
            self.assertEqual(len(commands), 2)
            self.assertNotIn("resume", commands[1])

    def test_batch_replay_is_bound_to_exact_role_and_backend(self):
        with tempfile.TemporaryDirectory() as td:
            batch = Path(td) / "batch"
            recorder = DecisionRecorder(build_model_client(config()), Path(td))
            recorder.begin_session(research_id="R1")
            recorder.bind_batch(batch)
            first = recorder.complete_json("attribute_results", {"fixture": "evidence"})
            self.assertEqual(first["summary"], "Synthetic experiment only")
            same = DecisionRecorder(build_model_client(config()), Path(td))
            same.begin_session(research_id="R1")
            same.bind_batch(batch)
            self.assertEqual(same.complete_json("attribute_results", {"fixture": "evidence"}), first)
            self.assertEqual(len(list((Path(td) / "logs" / "model_calls").glob("*.record.json"))), 1)
            switched = config()
            switched["backends"]["replacement"] = {
                "provider": "mock", "responses": {
                    "attribute_results": {"summary": "new provider", "findings": []}
                }
            }
            switched["roles"]["research_executor"]["backend"] = "replacement"
            next_recorder = DecisionRecorder(build_model_client(switched), Path(td))
            next_recorder.begin_session(research_id="R1")
            next_recorder.bind_batch(batch)
            with self.assertRaisesRegex(ModelError, "role/backend"):
                next_recorder.complete_json("attribute_results", {"fixture": "evidence"})
            self.assertEqual(len(list((Path(td) / "logs" / "model_calls").glob("*.record.json"))), 1)

    def test_batch_replay_rejects_modified_stored_model_response(self):
        with tempfile.TemporaryDirectory() as td:
            batch = Path(td) / "batch"
            initial = DecisionRecorder(build_model_client(config()), Path(td))
            initial.begin_session(research_id="R1")
            initial.bind_batch(batch)
            initial.complete_json("attribute_results", {"fixture": "evidence"})
            saved, = list((batch / "model_calls").glob("*.response.json"))
            payload = json.loads(saved.read_text(encoding="utf-8"))
            payload["result"]["summary"] = "untrusted replacement"
            saved.write_text(json.dumps(payload), encoding="utf-8")
            replay = DecisionRecorder(build_model_client(config()), Path(td))
            replay.begin_session(research_id="R1")
            replay.bind_batch(batch)
            with self.assertRaisesRegex(ModelError, "checksum mismatch"):
                replay.complete_json("attribute_results", {"fixture": "evidence"})

    def test_offline_cycle(self):
        gw = gateway()
        report = OfflineResearchCycle(build_model_client(config()), gw).run_once(
            research_id="R1", objective="test", region="CHN")
        self.assertEqual(report["decision"]["action"], "INVESTIGATE")
        self.assertEqual(gw.invocations, ["E1"])
        self.assertEqual(report["candidate_qualification"], "NOT_EVALUATED")
        self.assertFalse(report["simulation_submitted"])
        self.assertFalse(report["alpha_submitted"])

    def test_live_gateway_rejected(self):
        class LiveGateway:
            def execute(self, experiment):
                raise AssertionError("must never be called")
        with self.assertRaises(ResearchContractError):
            OfflineResearchCycle(build_model_client(config()), LiveGateway())

    def test_cross_region_experiment_fails_closed(self):
        cfg = config()
        cfg["backends"]["codex_mock"]["responses"]["plan_experiments"]["experiments"][0]["region"] = "USA"
        with self.assertRaises(ResearchContractError):
            OfflineResearchCycle(build_model_client(cfg), gateway()).run_once(
                research_id="R1", objective="test", region="CHN")

    def test_unknown_evidence_reference_rejected(self):
        cfg = config()
        cfg["backends"]["codex_mock"]["responses"]["research_decide"]["evidence_refs"] = [
            "live:unknown"
        ]
        with self.assertRaises(ResearchContractError):
            OfflineResearchCycle(build_model_client(cfg), gateway()).run_once(
                research_id="R1", objective="test", region="CHN")

    def test_executor_may_not_inject_unapproved_experiments(self):
        cfg = config()
        cfg["backends"]["agy_mock"]["responses"]["prepare_execution"]["experiment_ids"] = [
            "E1", "UNAUTHORIZED"
        ]
        gw = gateway()
        with self.assertRaisesRegex(ResearchContractError, "unauthorized"):
            OfflineResearchCycle(build_model_client(cfg), gw).run_once(
                research_id="R1", objective="test", region="CHN")
        self.assertEqual(gw.invocations, [])

    def test_missing_fixture_blocks_experiment(self):
        with self.assertRaises(ResearchContractError):
            OfflineResearchCycle(
                build_model_client(config()), OfflineSimulationGateway({})
            ).run_once(research_id="R1", objective="test", region="CHN")

    def test_mock_cannot_qualify_candidate(self):
        cfg = config()
        cfg["backends"]["codex_mock"]["responses"]["research_decide"]["action"] = "CANDIDATE"
        report = OfflineResearchCycle(build_model_client(cfg), gateway()).run_once(
            research_id="R1", objective="test", region="CHN")
        self.assertEqual(report["decision"]["action"], "CANDIDATE")
        self.assertEqual(report["candidate_qualification"], "NOT_EVALUATED")

    def test_invalid_budget_rejected(self):
        with self.assertRaises(ResearchContractError):
            OfflineResearchCycle(build_model_client(config()), gateway(), max_experiments=0)


if __name__ == "__main__":
    unittest.main()
