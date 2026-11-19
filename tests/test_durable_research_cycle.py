"""Offline, deterministic fault injection for multi-round research recovery."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from alpha_research.agents.configuration import AgentConfigError
from alpha_research.integrations.model import build_model_client
from alpha_research.research_cycle.contracts import ResearchContractError
from alpha_research.research_cycle.durable import DurableOfflineResearchRunner
from alpha_research.research_cycle.engine import OfflineSimulationGateway

ROOT = Path(__file__).resolve().parent.parent


def fixture_config():
    return json.loads((ROOT / "config.mock.example.json").read_text(encoding="utf-8"))["model"]


def two_round_config():
    cfg = fixture_config()
    lead = cfg["backends"]["codex_mock"]["responses"]
    executor = cfg["backends"]["agy_mock"]["responses"]
    first = copy.deepcopy(lead["plan_experiments"])
    second = copy.deepcopy(first)
    second["experiments"][0]["experiment_id"] = "E2"
    second["experiments"][0]["expression"] = "rank(ts_delta(close, 3))"
    second["experiments"][0]["hypothesis"] = "Compare rate-of-change signal"
    lead["plan_experiments"] = [first, second]
    lead["research_decide"] = [
        {"action": "OPTIMIZE", "reason": "Verify alternative lookback",
         "evidence_refs": ["offline:R1:E1"]},
        {"action": "STOP", "reason": "End synthetic experiment",
         "evidence_refs": ["offline:R1:round2:E2"]},
    ]
    executor["prepare_execution"] = [
        {"experiment_ids": ["E1"]}, {"experiment_ids": ["E2"]},
    ]
    executor["attribute_results"] = [
        {"summary": "Synthetic first round", "findings": [
            {"alpha_id": "E1", "classification": "insufficient_evidence",
             "reason": "Mock fixture only"}]},
        {"summary": "Synthetic second round", "findings": [
            {"alpha_id": "E2", "classification": "insufficient_evidence",
             "reason": "Mock fixture only"}]},
    ]
    return cfg


def simulated_gateway():
    return OfflineSimulationGateway({
        "E1": {"source": "offline_fixture", "status": "COMPLETED",
               "metrics": {"sharpe": 0.4, "fitness": 0.2}},
        "E2": {"source": "offline_fixture", "status": "COMPLETED",
               "metrics": {"sharpe": 0.5, "fitness": 0.3}},
    })


class TestDurableResearchCycle(unittest.TestCase):
    def test_two_round_optimized_flow(self):
        with tempfile.TemporaryDirectory() as root:
            gw = simulated_gateway()
            runner = DurableOfflineResearchRunner(
                build_model_client(two_round_config()), gw, root,
                max_rounds=3, max_total_experiments=3, max_per_round=1,
            )
            state = runner.run(research_id="R1", objective="mock study", region="CHN")
            self.assertEqual(state["status"], "COMPLETED")
            self.assertEqual(state["stop_reason"], "STOP")
            self.assertEqual(state["used_experiments"], 2)
            self.assertEqual(len(state["rounds"]), 2)
            self.assertEqual(gw.invocations, ["E1", "E2"])
            self.assertEqual(state["candidate_qualification"], "NOT_EVALUATED")
            self.assertFalse(state["simulation_submitted"])
            self.assertFalse(state["alpha_submitted"])
            resumed = runner.run(research_id="R1", objective="mock study", region="CHN")
            self.assertEqual(resumed, state)
            self.assertEqual(gw.invocations, ["E1", "E2"])

    def test_immutable_scope(self):
        with tempfile.TemporaryDirectory() as root:
            runner = DurableOfflineResearchRunner(
                build_model_client(fixture_config()), simulated_gateway(), root,
                max_rounds=1)
            runner.run(research_id="R1", objective="original", region="CHN")
            with self.assertRaisesRegex(ResearchContractError, "scope"):
                runner.run(research_id="R1", objective="changed", region="CHN")

    def test_budget_stops_before_unapproved_round(self):
        with tempfile.TemporaryDirectory() as root:
            gw = simulated_gateway()
            runner = DurableOfflineResearchRunner(
                build_model_client(two_round_config()), gw, root,
                max_rounds=3, max_total_experiments=1, max_per_round=1)
            state = runner.run(research_id="R1", objective="limited", region="CHN")
            self.assertEqual(state["status"], "BUDGET_EXHAUSTED")
            self.assertEqual(state["used_experiments"], 1)
            self.assertEqual(gw.invocations, ["E1"])

    def test_global_expression_deduplication(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = two_round_config()
            duplicate = cfg["backends"]["codex_mock"]["responses"]["plan_experiments"][1]
            duplicate["experiments"][0]["expression"] = "-rank(close)"
            gw = simulated_gateway()
            runner = DurableOfflineResearchRunner(
                build_model_client(cfg), gw, root, max_rounds=3,
                max_total_experiments=3, max_per_round=1)
            with self.assertRaisesRegex(ResearchContractError, "duplicates"):
                runner.run(research_id="R1", objective="repeat", region="CHN")
            self.assertEqual(gw.invocations, ["E1"])
            state = runner.load("R1")
            self.assertEqual(state["status"], "BLOCKED")
            self.assertEqual(state["used_experiments"], 1)

    def test_unresolved_intent_does_not_repost_on_restart(self):
        with tempfile.TemporaryDirectory() as root:
            gw = simulated_gateway()
            calls = []
            def uncertain(experiment):
                calls.append(experiment.experiment_id)
                raise OSError("fixture response lost")
            gw.execute = uncertain
            runner = DurableOfflineResearchRunner(
                build_model_client(fixture_config()), gw, root,
                max_rounds=1)
            with self.assertRaisesRegex(OSError, "response lost"):
                runner.run(research_id="R1", objective="uncertain", region="CHN")
            state = runner.load("R1")
            self.assertEqual(state["status"], "BLOCKED")
            intent = state["rounds"][0]["simulations"]["E1"]
            self.assertEqual(intent["status"], "IN_FLIGHT")
            with self.assertRaisesRegex(ResearchContractError, "reconciliation"):
                runner.run(research_id="R1", objective="uncertain", region="CHN",
                           resume_blocked=True)
            self.assertEqual(calls, ["E1"])
            runner.reconcile_offline_fixture("R1", "E1")
            state = runner.run(research_id="R1", objective="uncertain", region="CHN",
                               resume_blocked=True)
            self.assertEqual(state["status"], "BUDGET_EXHAUSTED")
            self.assertEqual(calls, ["E1"])
            self.assertEqual(state["rounds"][0]["simulations"]["E1"]["status"],
                             "COMPLETED")

    def test_blocked_invalid_executor_plan_has_no_simulation(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = fixture_config()
            cfg["backends"]["agy_mock"]["responses"]["prepare_execution"] = {
                "experiment_ids": ["NOT_AUTHORIZED"]
            }
            gw = simulated_gateway()
            runner = DurableOfflineResearchRunner(
                build_model_client(cfg), gw, root, max_rounds=1)
            with self.assertRaisesRegex(ResearchContractError, "unauthorized"):
                runner.run(research_id="R1", objective="invalid", region="CHN")
            self.assertEqual(gw.invocations, [])
            self.assertEqual(runner.load("R1")["status"], "BLOCKED")

    def test_exhausted_mock_response_fails_closed(self):
        model = build_model_client(two_round_config())
        with self.assertRaises(AgentConfigError):
            model.complete_json("plan_experiments", {"round_index": 5})

    def test_model_routing_profile_is_immutable_on_resume(self):
        with tempfile.TemporaryDirectory() as root:
            first = DurableOfflineResearchRunner(
                build_model_client(fixture_config()), simulated_gateway(), root,
                max_rounds=1)
            first.run(research_id="R1", objective="pinned", region="CHN")
            changed = fixture_config()
            changed["task_roles"]["prepare_execution"] = "research_lead"
            second = DurableOfflineResearchRunner(
                build_model_client(changed), simulated_gateway(), root,
                max_rounds=1)
            with self.assertRaisesRegex(ResearchContractError, "scope"):
                second.run(research_id="R1", objective="pinned", region="CHN")

    def test_explicit_invalid_checkpoint_can_be_regenerated(self):
        from alpha_research.agents.router import RoleRoutedModelClient, MockModelClient
        cfg = fixture_config()
        calls = {"prepare": 0}
        class FlakyExecutor(MockModelClient):
            def complete_json(self, task, context, *, thread_path=None):
                if task == "prepare_execution":
                    calls["prepare"] += 1
                    if calls["prepare"] == 1:
                        return {"experiment_ids": ["INVALID"]}
                return super().complete_json(task, context, thread_path=thread_path)
        def backend_factory(backend):
            if backend is not None and backend.get("provider") == "mock" and (
                "prepare_execution" in backend.get("responses", {})):
                return FlakyExecutor(backend)
            return MockModelClient(backend)
        with tempfile.TemporaryDirectory() as root:
            model = RoleRoutedModelClient(cfg, backend_factory=backend_factory)
            runner = DurableOfflineResearchRunner(model, simulated_gateway(), root, max_rounds=1)
            with self.assertRaises(ResearchContractError):
                runner.run(research_id="R1", objective="fix", region="CHN")
            with self.assertRaisesRegex(ResearchContractError, "blocked"):
                runner.run(research_id="R1", objective="fix", region="CHN")
            state = runner.invalidate_model_checkpoint(
                "R1", "prepare_execution", "Executor returned unauthorized ID")
            self.assertEqual(len(state["checkpoint_invalidations"]), 1)
            state = runner.run(research_id="R1", objective="fix", region="CHN",
                               resume_blocked=True)
            self.assertEqual(calls["prepare"], 2)
            self.assertEqual(state["status"], "BUDGET_EXHAUSTED")
            self.assertEqual(state["used_experiments"], 1)

    def test_reconciliation_validates_offline_fixture(self):
        with tempfile.TemporaryDirectory() as root:
            gw = simulated_gateway()
            def interrupted(experiment):
                raise RuntimeError("unknown execution outcome")
            gw.execute = interrupted
            runner = DurableOfflineResearchRunner(
                build_model_client(fixture_config()), gw, root, max_rounds=1)
            with self.assertRaises(RuntimeError):
                runner.run(research_id="R1", objective="unknown", region="CHN")
            gw.fixtures["E1"] = {"source": "offline_fixture", "status": "COMPLETED"}
            with self.assertRaisesRegex(ResearchContractError, "metrics"):
                runner.reconcile_offline_fixture("R1", "E1")
            self.assertEqual(runner.load("R1")["rounds"][0]["simulations"]["E1"]["status"],
                             "IN_FLIGHT")

    def test_reject_non_offline_runner_gateway(self):

        class Imposter:
            is_offline_mock = True
            def execute(self, experiment):
                raise AssertionError("must never be called")
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ResearchContractError):
                DurableOfflineResearchRunner(build_model_client(fixture_config()),
                                             Imposter(), root)


if __name__ == "__main__":
    unittest.main()
