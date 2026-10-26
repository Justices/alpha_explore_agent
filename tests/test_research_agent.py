import json
from copy import deepcopy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nastu_quant.agent import ResearchAgent, ResearchError
from nastu_quant.config import DEFAULT_CONFIG


class ScriptedModel:
    def __init__(self, decisions):
        self.decisions = decisions
        self.tasks = []
        self.contexts = {}

    def complete_json(self, task, context):
        self.tasks.append(task)
        self.contexts[task] = context
        return self.decisions[task]


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.probe_calls_remaining = 0
        self.fail_simulation = False
        self.recent_alphas = []
        self.ra_result = "PASS"
        self.catalog_incomplete = False

    def call(self, cmd, **params):
        self.calls.append((cmd, params))
        if cmd == "get_platform_setting_options":
            return {"instrument_options": [{"InstrumentType": "EQUITY",
                     "Region": "GLB", "Delay": 1,
                     "Universe": ["TOP3000"], "Neutralization": ["MARKET"]}]}
        if cmd == "get_datasets":
            return [{"id": "risk60"}]
        if cmd == "get_datafields":
            rows = [
                {"id": "signal_a", "type": "MATRIX", "dataset": {"id": "risk60"},
                 "description": "slow economic signal", "coverage": 0.9},
                {"id": "signal_b", "type": "MATRIX", "dataset": {"id": "risk60"},
                 "description": "independent slow signal", "coverage": 0.8},
            ]
            if not self.catalog_incomplete:
                for row in rows:
                    row.update({"zeroFraction": 0.05, "updateFrequency": "monthly",
                                "units": "score", "median": 0.4,
                                "distribution": "bounded", "publicationLag": 1,
                                "tradeability": "broad"})
            return {"results": rows}
        if cmd == "get_operators":
            return [{"name": "rank"}, {"name": "ts_mean"}]
        if cmd == "create_multi_simulation":
            if self.fail_simulation:
                raise RuntimeError("driver lost after POST")
            if self.probe_calls_remaining:
                self.probe_calls_remaining -= 1
                return {"alpha_results": [
                    {"alpha_id": "P1", "details": {"is": {"longCount": 100}}},
                    {"alpha_id": "P2", "details": {"is": {"longCount": 60}}},
                ]}
            return {"alpha_results": [
                {"alpha_id": "A1", "details": {"is": {"sharpe": 1.7}}},
                {"alpha_id": "A2", "details": {"is": {"sharpe": 1.4}}},
            ]}
        if cmd == "get_alpha_details":
            return {"id": params["alpha_id"], "is": {"sharpe": 1.7}}
        if cmd == "get_submission_check":
            return {"checks": [{"name": "LOW_SHARPE", "result": self.ra_result}]}
        if cmd == "check_correlation":
            return {"checks": {
                "production": {"correlation_data": {"max": 0.5}},
                "self": {"correlation_data": {
                    "count": 1, "next": None,
                    "schema": {"properties": [{"name": "alphaId"},
                                              {"name": "correlation"}]},
                    "records": [["OTHER", 0.3]],
                }},
            }}
        if cmd == "get_user_alphas":
            return self.recent_alphas
        raise AssertionError(cmd)


class TestResearchAgent(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gateway = FakeGateway()
        self.config = deepcopy(DEFAULT_CONFIG)
        self.config["scope"]["region"] = "GLB"
        self.config["scope"]["universe"] = "TOP3000"
        self.config["scope"]["delay"] = 1
        self.config["scope"]["decay"] = 0
        self.config["scope"]["neutralization"] = "MARKET"
        self.config["scope"]["truncation"] = 0.08
        self.config["scope"]["pasteurization"] = "ON"
        self.model = ScriptedModel({
            "select_scope": {"delay": 1, "universe": "TOP3000",
                             "neutralization": "MARKET", "reason": "supported broad pool"},
            "plan_probes": {"core_fields": ["signal_a", "signal_b"],
                            "probes": [], "reason": "catalog evidence sufficient"},
            "construct_batch": {
                "hypothesis": "Persistent lender signal predicts future returns",
                "expected_sign": "negative",
                "structure_window_rationale": "slow level, no short delta",
                "failure_criterion": "No stable cross-sectional return spread",
                "field_report": [
                    {"field_id": field, "verdict": "ADMIT",
                     "constraints": ["use a slow level structure"],
                     "evidence": {dimension: {"source": "catalog", "observation": "observed"}
                                  for dimension in ("nominal_coverage", "usable_observations",
                                                    "refresh_rate", "scale_units",
                                                    "typical_level", "distribution")}}
                    for field in ("signal_a", "signal_b")],
                "variants": [
                    {"expression": "rank(signal_a)", "reason": "level baseline"},
                    {"expression": "rank(signal_b)", "reason": "independent level control"},
                ],
            },
            "attribute_results": {"summary": "A2 below headline threshold"},
        })

    def test_run_once_saves_prereg_before_single_platform_batch(self):
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        result = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(result["dataset_id"], "risk60")
        self.assertEqual(result["alpha_ids"], ["A1", "A2"])
        self.assertEqual(self.model.tasks,
                         ["plan_probes", "construct_batch", "attribute_results"])
        calls = [name for name, _ in self.gateway.calls]
        self.assertEqual(calls.count("create_multi_simulation"), 1)
        self.assertNotIn("submit_alpha", calls)
        batch = next(Path(self.tmp.name).rglob("prereg.json")).parent
        self.assertEqual(batch.parts[-5:-1], ("GLB", "D1", "TOP3000", "risk60"))
        prereg = json.loads((batch / "prereg.json").read_text(encoding="utf-8"))
        self.assertEqual(len(prereg["variants"]), 2)
        self.assertTrue((batch / "results.json").exists())

    def test_invalid_expression_blocks_simulation(self):
        self.model.decisions["construct_batch"]["variants"][0]["expression"] = "rank(unknown_field)"
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertNotIn("create_multi_simulation", [name for name, _ in self.gateway.calls])

    def test_probe_simulation_precedes_alpha_and_keeps_diagnostic_settings(self):
        self.gateway.probe_calls_remaining = 1
        self.model.decisions["plan_probes"] = {"core_fields": ["signal_a", "signal_b"], "probes": [
            {"field_id": "signal_a", "expression": "signal_a", "question": "nominal coverage",
             "measures": ["nominal_coverage"],
             "economic_relevance": "can represent the target stock pool",
             "expected": "many observations", "failure_criterion": "few observations"},
            {"field_id": "signal_a", "expression": "signal_a != 0 ? 1 : 0", "question": "effective coverage",
             "measures": ["usable_observations"],
             "economic_relevance": "zero fill may distort ranking",
             "expected": "mostly nonzero", "failure_criterion": "mostly zero"},
        ]}
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        result = agent.run_once("slow lending signal", dataset_id="risk60")
        sims = [params for name, params in self.gateway.calls
                if name == "create_multi_simulation"]
        self.assertEqual(len(sims), 2)
        self.assertEqual(sims[0]["neutralization"], "NONE")
        self.assertEqual(sims[1]["neutralization"], "MARKET")
        self.assertEqual(result["alpha_ids"], ["A1", "A2"])
        self.assertEqual(len(self.model.contexts["construct_batch"]["probe_results"]), 2)
        self.assertNotIn("P1", result["alpha_ids"])

    def test_missing_region_blocks_before_platform_access(self):
        self.config["scope"]["region"] = None
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(self.gateway.calls, [])

    def test_lost_response_is_reconciled_without_reposting(self):
        self.gateway.fail_simulation = True
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(RuntimeError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        batch = next(Path(self.tmp.name).rglob("prereg.json")).parent
        intent = json.loads((batch / "simulation_intent.json").read_text(encoding="utf-8"))
        self.assertEqual(intent["status"], "SUBMITTING")
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.gateway.recent_alphas = [
            {"id": "A1", "regular": {"code": "rank(signal_a)"},
             "settings": intent["settings"]},
            {"id": "A2", "regular": {"code": "rank(signal_b)"},
             "settings": intent["settings"]},
        ]
        recovered = agent.recover(batch)
        self.assertEqual(recovered["alpha_ids"], ["A1", "A2"])
        self.assertEqual(sum(name == "create_multi_simulation"
                             for name, _ in self.gateway.calls), 1)

    def test_ra_failure_never_reaches_correlation(self):
        self.gateway.ra_result = "FAIL"
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        result = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertNotIn("check_correlation", [name for name, _ in self.gateway.calls])
        self.assertEqual(result["gate"]["A1"]["status"], "RA_FAILED")

    def test_zero_ra_failures_record_raw_correlation_evidence(self):
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        result = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(sum(name == "check_correlation"
                             for name, _ in self.gateway.calls), 2)
        self.assertEqual(result["gate"]["A1"]["correlations"]["prod"], 0.5)

    def test_stop_flag_blocks_new_simulation(self):
        stop_flag = Path(self.tmp.name) / "stop.flag"
        stop_flag.write_text("stop\n", encoding="utf-8")
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name), stop_flag=stop_flag)
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertNotIn("create_multi_simulation", [name for name, _ in self.gateway.calls])

    def test_missing_hypothesis_direction_blocks_alpha_batch(self):
        del self.model.decisions["construct_batch"]["expected_sign"]
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertNotIn("create_multi_simulation", [name for name, _ in self.gateway.calls])

    def test_incomplete_catalog_without_probes_blocks_construction(self):
        self.gateway.catalog_incomplete = True
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertNotIn("construct_batch", self.model.tasks)

    def test_missing_universe_is_selected_from_live_options(self):
        self.config["scope"]["universe"] = None
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        result = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(result["dataset_id"], "risk60")
        self.assertIn("select_scope", self.model.tasks)

    def test_invalid_scope_choice_blocks_before_dataset_request(self):
        self.config["scope"]["universe"] = None
        self.model.decisions["select_scope"]["universe"] = "MADE_UP"
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertNotIn("get_datasets", [name for name, _ in self.gateway.calls])


if __name__ == "__main__":
    unittest.main()
