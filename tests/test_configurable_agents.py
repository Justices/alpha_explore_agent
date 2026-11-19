"""Offline contract tests; no provider SDK or BRAIN service is called."""
import json
import tempfile
import unittest
from pathlib import Path

from alpha_research.agents.antigravity import (
    AntigravityAdapterError, AntigravityModelClient, decode_json_object
)
from alpha_research.agents.configuration import AgentConfigError
from alpha_research.integrations.model import build_model_client, DecisionRecorder
from alpha_research.research_cycle import OfflineResearchCycle, OfflineSimulationGateway
from alpha_research.research_cycle.contracts import ResearchContractError

ROOT = Path(__file__).resolve().parent.parent


def config():
    return json.loads((ROOT / "config.mock.example.json").read_text(encoding="utf-8"))["model"]


def gateway():
    fixtures = ROOT / "tests" / "fixtures" / "research_cycle_simulations.json"
    return OfflineSimulationGateway(json.loads(fixtures.read_text(encoding="utf-8")))


class TestConfigurableAgents(unittest.TestCase):
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
