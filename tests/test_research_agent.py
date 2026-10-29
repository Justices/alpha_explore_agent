import json
from copy import deepcopy
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nastu_quant.agent import ResearchAgent, ResearchError
from nastu_quant.platform_runtime import PlatformRuntime
from nastu_quant.config import DEFAULT_CONFIG
from nastu_quant.recovery import SETTING_KEYS


class ScriptedModel:
    def __init__(self, decisions):
        self.decisions = decisions
        self.tasks = []
        self.contexts = {}
        self.fail_task_once = None

    def complete_json(self, task, context):
        self.tasks.append(task)
        self.contexts[task] = context
        if self.fail_task_once == task:
            self.fail_task_once = None
            raise RuntimeError("model response lost")
        decision = self.decisions[task]
        result = deepcopy(decision.pop(0) if isinstance(decision, list) else decision)
        if task == "plan_probes":
            for probe in result.get("probes", []):
                probe.setdefault("research", {
                    key: "Predeclared diagnostic rationale"
                    for key in ("idea", "data_rationale", "operator_rationale",
                                "field_relationship", "settings_rationale",
                                "expected_behavior", "failure_criterion")})
        return result


class FakeGateway:
    def __init__(self, base):
        self.base = Path(base) / "fake_platform"
        self.calls = []
        self.simulations = {}
        self.probe_calls_remaining = 0
        self.fail_simulation = False
        self.fail_probe_once = False
        self.recent_alphas = []
        self.ra_result = "PASS"
        self.ra_by_alpha = {}
        self.unavailable_details = set()
        self.settings_by_alpha = {}
        self.catalog_incomplete = False
        self.datasets = ["risk60"]
        self.single_ids = []
        self.full_details = False
        self.sharpe_by_alpha = {}
        self.correlation_by_alpha = {}
        self.correlation_unavailable = set()
        self.prod_by_alpha = {}
        self.local_self_by_alpha = {}
        self.allow_probe_neutralization = True
        self.omit_field_dataset = False
        self.omit_field_description = False
        self.single_probe_counts = False
        self.dataset_detail_scope_mismatch = False

    def _details(self, alpha_id):
        if alpha_id in self.unavailable_details:
            raise RuntimeError("Alpha read-back unavailable")
        ra_result = self.ra_by_alpha.get(alpha_id, self.ra_result)
        if self.full_details:
            is_data = {"sharpe": 1.8, "fitness": 1.2, "sharpe_2y": 1.7,
                       "margin": 0.001, "turnover": 0.2,
                       "checks": [{"name": "LOW_SHARPE", "result": ra_result}]}
        else:
            is_data = {"sharpe": 1.7,
                       "checks": [{"name": "LOW_SHARPE", "result": ra_result}]}
        if alpha_id in self.sharpe_by_alpha:
            is_data["sharpe"] = self.sharpe_by_alpha[alpha_id]
        details = {"id": alpha_id, "is": is_data}
        simulation = self.simulations.get(alpha_id)
        if simulation:
            details.update({"type": "REGULAR", "regular": {"code": simulation["expression"]},
                            "settings": {SETTING_KEYS.get(key, key): value for key, value in
                                         simulation["settings"].items()}})
            details["settings"].update(self.settings_by_alpha.get(alpha_id, {}))
        return details

    def call(self, cmd, **params):
        self.calls.append((cmd, params))
        if cmd == "set_case":
            self.case_key, self.run_id = params["case_key"], params["run_id"]
            return {"case_key": self.case_key, "run_id": self.run_id}
        if cmd == "validate_simulation_settings":
            return PlatformRuntime.validate_simulation_settings(params["settings"])
        if cmd == "get_platform_setting_options":
            return {"instrument_options": [{"InstrumentType": "EQUITY",
                     "Region": "GLB", "Delay": 1,
                     "Universe": ["TOP3000"], "Neutralization": (
                         ["MARKET", "NONE"] if self.allow_probe_neutralization
                         else ["MARKET"])}]}
        if cmd == "get_simulation_settings_schema":
            return {"decay": {"type": "integer", "minValue": 0, "maxValue": 512},
                    "truncation": {"type": "float", "minValue": 0, "maxValue": 1},
                    "pasteurization": {"type": "choice", "choices": [
                        {"value": "ON"}, {"value": "OFF"}]},
                    "unitHandling": {"type": "choice", "choices": [{"value": "VERIFY"}]},
                    "nanHandling": {"type": "choice", "choices": [
                        {"value": "ON"}, {"value": "OFF"}]},
                    "maxTrade": {"type": "choice", "choices": [
                        {"value": "ON"}, {"value": "OFF"}]},
                    "testPeriod": {"type": "string", "minValue": "P0Y0M0D",
                                   "maxValue": "P6Y0M0D"}}
        if cmd == "get_datasets":
            return [{"id": dataset} for dataset in self.datasets]
        if cmd == "get_dataset_details":
            return {"id": params["dataset_id"], "data": [{
                "region": "USA" if self.dataset_detail_scope_mismatch else "GLB",
                "delay": 1, "universe": "TOP3000"}]}
        if cmd == "get_datafields":
            rows = [
                {"id": "signal_a", "type": "MATRIX", "dataset": {"id": params["dataset_id"]},
                 "description": "slow economic signal", "coverage": 0.9},
                {"id": "signal_b", "type": "MATRIX", "dataset": {"id": params["dataset_id"]},
                 "description": "independent slow signal", "coverage": 0.8},
            ]
            if not self.catalog_incomplete:
                for row in rows:
                    row.update({"zeroFraction": 0.05, "updateFrequency": "monthly",
                                "units": "score", "median": 0.4,
                                "distribution": "bounded", "publicationLag": 1,
                                "tradeability": "broad"})
            if self.omit_field_dataset:
                for row in rows:
                    row.pop("dataset")
            if self.omit_field_description:
                for row in rows:
                    row.pop("description")
            return {"results": rows}
        if cmd == "get_operators":
            if any(key not in params for key in ("region", "universe", "delay")):
                raise AssertionError("operator catalog requires the active scope")
            return [{"name": "rank", "scope": ["REGULAR"], "definition": "rank(x, rate=2)"},
                    {"name": "ts_mean", "scope": ["REGULAR"], "definition": "ts_mean(x, d)"},
                    {"name": "combo_a", "scope": ["COMBO"], "definition": "combo_a(x)"}]
        if cmd == "simulate_batch":
            is_probe = params["label"].startswith("probe_")
            if is_probe and self.fail_probe_once:
                self.fail_probe_once = False
                raise RuntimeError("probe driver lost after POST")
            if self.fail_simulation:
                raise RuntimeError("driver lost after POST")
            variants = params["variants"]
            if is_probe and self.probe_calls_remaining:
                self.probe_calls_remaining -= 1
            rows = []
            for index, variant in enumerate(variants):
                alpha_id = (self.single_ids.pop(0) if len(variants) == 1 and self.single_ids
                            else f"{'P' if is_probe else 'A'}{index + 1}")
                metrics = ({"longCount": 120 if self.single_probe_counts else 100 - index * 40,
                            "shortCount": 100} if is_probe else
                           {"sharpe": 1.7 if index == 0 else 1.4})
                rows.append({"alpha_id": alpha_id, "is": metrics,
                             "name": variant["name"], "expression": variant["expression"],
                             "success": True})
                self.simulations[alpha_id] = {"expression": variant["expression"],
                                              "settings": params["settings"],
                                              "dataset": params["dataset_id"]}
            folder = self.base / self.case_key / self.run_id
            folder.mkdir(parents=True, exist_ok=True)
            output = folder / f"{params['batch_id']}_{params['label']}.json"
            output.write_text(json.dumps(rows), encoding="utf-8")
            output.with_suffix(".admission.json").write_text(
                json.dumps({"variants": variants}), encoding="utf-8")
            return {"results": rows, "runner": {"case_key": self.case_key,
                    "run_id": self.run_id, "output": str(output),
                    "field_cache_root": str(folder / "fields")}}
        if cmd == "resume_simulation_batch":
            raise RuntimeError("no saved runner result")
        if cmd == "audit_alphas":
            results = []
            for alpha_id in params["alpha_ids"]:
                simulation = self.simulations.get(alpha_id)
                if not simulation:
                    results.append({"alpha_id": alpha_id, "candidate_eligible": False})
                    continue
                settings = simulation["settings"]
                field_ids = [field for field in ("signal_a", "signal_b")
                             if field in simulation["expression"]]
                pairs = self.correlation_by_alpha.get(alpha_id, [["OTHER", 0.3]])
                results.append({"alpha_id": alpha_id, "candidate_eligible": True,
                                "candidate_status": "candidate", "gate_failures": [],
                                "gate_pending": [], "candidate_metric_failures": [],
                                "raw_details": self._details(alpha_id),
                                "expression": simulation["expression"],
                                "prod_corr": self.prod_by_alpha.get(alpha_id, 0.5),
                                "self_corr": max(abs(row[1]) for row in pairs),
                                "pairwise_state": "not_applicable", "pairwise": [],
                                "field_admission": {"passed": True, "failures": [],
                                    "dataset": simulation["dataset"],
                                    "scope": {key: settings[key] for key in
                                              ("region", "universe", "delay", "instrument_type")},
                                    "fields": [{"id": field} for field in field_ids]}})
            return {"results": results, "audit_path": str(self.base / "audit.json")}
        if cmd == "get_alpha_details":
            return self._details(params["alpha_id"])
        if cmd == "get_submission_check":
            return {"checks": [{"name": "LOW_SHARPE", "result": self.ra_result}]}
        if cmd == "check_local_self_correlation":
            return self.local_self_by_alpha.get(params["alpha_id"], {
                "status": "pass", "value": 0.3, "complete": True,
                "comparison_count": 1, "window_years": 4, "limit": params["self_corr_max"]})
        if cmd == "check_correlation":
            if params["alpha_id"] in self.correlation_unavailable:
                raise RuntimeError("correlation unavailable")
            pairs = self.correlation_by_alpha.get(params["alpha_id"],
                                                   [["OTHER", 0.3]])
            return {"checks": {
                "production": {"correlation_data": {"max": self.prod_by_alpha.get(params["alpha_id"], 0.5)}},
                "self": {"correlation_data": {
                    "count": len(pairs), "next": None,
                    "schema": {"properties": [{"name": "alphaId"},
                                              {"name": "correlation"}]},
                    "records": pairs,
                }},
            }}
        if cmd == "get_user_alphas":
            return self.recent_alphas
        raise AssertionError(cmd)


class TestResearchAgent(unittest.TestCase):
    def setUp(self):
        self.tmp_parent = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_parent.cleanup)
        self.tmp = SimpleNamespace(name=str(Path(self.tmp_parent.name) / "runs"))
        Path(self.tmp.name).mkdir()
        self.gateway = FakeGateway(self.tmp.name)
        self.config = deepcopy(DEFAULT_CONFIG)
        self.config["scope"]["region"] = "GLB"
        self.config["scope"]["universe"] = "TOP3000"
        self.config["scope"]["delay"] = 1
        self.config["scope"]["decay"] = 0
        self.config["scope"]["neutralization"] = "MARKET"
        self.config["scope"]["truncation"] = 0.08
        self.config["scope"]["pasteurization"] = "ON"
        self.config["scope"]["unit_handling"] = "VERIFY"
        self.config["scope"]["nan_handling"] = "OFF"
        self.config["scope"]["test_period"] = "P0Y0M"
        self.config["scope"]["max_trade"] = "OFF"
        self.model = ScriptedModel({
            "select_scope": {"delay": 1, "universe": "TOP3000",
                             "neutralization": "MARKET", "reason": "supported broad pool"},
            "plan_probes": {"core_fields": ["signal_a", "signal_b"],
                            "probes": [], "reason": "catalog evidence sufficient"},
            "construct_batch": {
                "hypothesis": "Persistent lender signal predicts future returns",
                "expected_sign": "negative",
                "structure_window_rationale": "slow level, no short delta",
                "data_rationale": "the fields represent a slow lending signal",
                "operator_rationale": "rank compares stocks within the universe",
                "field_relationship": "independent fields provide controls",
                "settings_rationale": "broad scope and fixed settings form a baseline",
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
            "review_candidate": {"verdict": "CLEAR", "reason": "metrics and checks are stable",
                                 "evidence_refs": ["readback", "correlation"]},
            "decide_next": {"action": "continue", "reason": "independent field branch"},
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
        self.assertEqual(calls.count("simulate_batch"), 1)
        self.assertNotIn("submit_alpha", calls)
        batch = next(Path(self.tmp.name).rglob("prereg.json")).parent
        self.assertEqual(batch.parts[-5:-1], ("GLB", "D1", "TOP3000", "risk60"))
        prereg = json.loads((batch / "prereg.json").read_text(encoding="utf-8"))
        self.assertEqual(len(prereg["variants"]), 2)
        self.assertTrue((batch / "results.json").exists())
        metering = json.loads((batch / "metering.json").read_text(encoding="utf-8"))
        self.assertEqual(metering["platform_batches"], 1)
        self.assertEqual(metering["alpha_expressions"], 2)
        self.assertIsNone(metering["model_tokens"])

    def test_ppa_objective_records_local_eligibility_separately_from_candidate(self):
        self.gateway.single_ids = ["A1"]
        self.gateway.full_details = True
        self.gateway.sharpe_by_alpha["A1"] = 1.0
        self.model.decisions["construct_batch"]["variants"] = [{
            "expression": "rank(" * 8 + "signal_a" + ")" * 8, "reason": "PPA boundary"}]
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        result = agent.run_once("探索 ppa 基本面因子", dataset_id="risk60")
        prereg = json.loads((Path(result["batch_dir"]) / "prereg.json").read_text(encoding="utf-8"))
        self.assertEqual(prereg["research_type"], "PPA")
        self.assertEqual(result["gate"]["A1"]["ppa_eligibility"]["status"], "PASS")
        self.assertNotEqual(result["gate"]["A1"]["status"], "CANDIDATE")
        self.assertEqual(self.model.contexts["construct_batch"]["research_type"], "PPA")

    def test_ppa_eligibility_is_pending_if_readback_expression_does_not_match(self):
        self.gateway.single_ids = ["A1"]
        self.gateway.full_details = True
        self.model.decisions["construct_batch"]["variants"] = [{
            "expression": "rank(signal_a)", "reason": "PPA baseline"}]
        original_details = self.gateway._details
        def changed_details(alpha_id):
            details = original_details(alpha_id)
            if "regular" in details:
                details["regular"]["code"] = "rank(signal_b)"
            return details
        self.gateway._details = changed_details
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        result = agent.run_once("探索 PPA", dataset_id="risk60")
        eligibility = result["gate"]["A1"]["ppa_eligibility"]
        self.assertEqual(eligibility["status"], "PENDING")
        self.assertIn("expression_unverified", eligibility["pending"])

    def test_ppa_over_eight_operators_never_reaches_simulation(self):
        self.model.decisions["construct_batch"]["variants"] = [{
            "expression": "rank(" * 9 + "signal_a" + ")" * 9, "reason": "too many operators"}]
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        with self.assertRaisesRegex(ResearchError, "E_OPERATOR_COUNT"):
            agent.run_once("探索 PPA", dataset_id="risk60")
        self.assertNotIn("simulate_batch", [name for name, _ in self.gateway.calls])

    def test_regular_objective_can_simulate_more_than_eight_operators(self):
        self.gateway.single_ids = ["A1"]
        self.gateway.full_details = True
        self.model.decisions["construct_batch"]["variants"] = [{
            "expression": "rank(" * 9 + "signal_a" + ")" * 9, "reason": "regular expression"}]
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        result = agent.run_once("探索基本面因子", dataset_id="risk60")
        self.assertEqual(result["gate"]["A1"]["status"], "CANDIDATE")
        self.assertNotIn("ppa_eligibility", result["gate"]["A1"])

    def test_ppa_bounded_run_reports_ppa_eligibility(self):
        self.gateway.single_ids = ["A1"]
        self.model.decisions["construct_batch"]["variants"] = [{
            "expression": "rank(signal_a)", "reason": "PPA baseline"}]
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        summary = agent.run_case("探索 PPA", max_batches=1, dataset_id="risk60")
        self.assertIn("A1", summary.get("ppa_eligibility", {}))
        self.assertEqual(summary["ppa_eligibility"]["A1"]["status"], "PASS")

    def test_ppa_bounded_run_does_not_report_stale_retained_eligibility(self):
        self.gateway.single_ids = ["A1", "A2"]
        self.gateway.full_details = True
        self.gateway.correlation_by_alpha = {"A1": [["OTHER", 0.3]], "A2": [["A1", 0.2]]}
        original_decide = self.model.complete_json
        def decide(task, context):
            if task == "construct_batch":
                field = "signal_b" if context.get("prior_batches") else "signal_a"
                self.model.decisions["construct_batch"]["variants"] = [
                    {"expression": f"rank({field})", "reason": "independent field"}]
            return original_decide(task, context)
        self.model.complete_json = decide
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        original_run = agent.run_once
        def run_and_change(*args, **kwargs):
            result = original_run(*args, **kwargs)
            self.gateway.sharpe_by_alpha["A1"] = 0.99
            return result
        agent.run_once = run_and_change
        summary = agent.run_case("探索 PPA", max_batches=2, dataset_id="risk60")
        self.assertEqual(summary["ppa_eligibility"]["A1"]["status"], "FAIL")
        self.assertIn("sharpe", summary["ppa_eligibility"]["A1"]["failures"])

    def test_ppa_recovery_preserves_locked_mode(self):
        stop_flag = Path(self.tmp.name) / "stop.flag"
        stop_flag.write_text("stop\n", encoding="utf-8")
        self.gateway.single_ids = ["A1"]
        self.model.decisions["construct_batch"]["variants"] = [{
            "expression": "rank(" * 8 + "signal_a" + ")" * 8, "reason": "PPA boundary"}]
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name), stop_flag=stop_flag)
        with self.assertRaises(ResearchError):
            agent.run_once("探索 PPA", dataset_id="risk60")
        batch = agent._unfinished_batch()
        stop_flag.unlink()
        result = agent.recover(batch)
        self.assertEqual(result["gate"]["A1"]["ppa_eligibility"]["status"], "PASS")
        self.assertEqual(sum(name == "simulate_batch" for name, _ in self.gateway.calls), 1)

    def test_fixed_dataset_detail_must_match_exact_live_scope(self):
        self.gateway.dataset_detail_scope_mismatch = True
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaisesRegex(ResearchError, "Dataset.*exact scope"):
            agent.run_once("slow lending signal", dataset_id="risk60")
        calls = [name for name, _ in self.gateway.calls]
        self.assertIn("get_dataset_details", calls)
        self.assertNotIn("get_datafields", calls)
        self.assertNotIn("simulate_batch", calls)

    def test_missing_effective_settings_are_selected_and_live_validated(self):
        for key in ("decay", "truncation", "pasteurization"):
            self.config["scope"][key] = None
        self.model.decisions["select_scope"].update({
            "decay": 3, "truncation": 0.08, "pasteurization": "ON",
            "reason": "slow fundamental changes merit modest decay and broad weights"})
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        scope = agent._scope("fundamental forecast")
        self.assertEqual((scope["decay"], scope["truncation"],
                          scope["pasteurization"]), (3, 0.08, "ON"))
        self.assertIn("get_simulation_settings_schema",
                      [name for name, _ in self.gateway.calls])

    def test_out_of_range_model_setting_blocks_dataset_access(self):
        self.config["scope"]["truncation"] = None
        self.model.decisions["select_scope"]["truncation"] = 1.2
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaisesRegex(ResearchError, "truncation.*out of range"):
            agent.run_once("fundamental forecast", dataset_id="risk60")
        self.assertNotIn("get_datasets", [name for name, _ in self.gateway.calls])

    def test_test_period_is_checked_against_live_schema(self):
        self.config["scope"]["test_period"] = "P7Y0M0D"
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        with self.assertRaisesRegex(ResearchError, "test_period.*live"):
            agent.run_once("fundamental forecast", dataset_id="risk60")
        self.assertNotIn("get_datasets", [name for name, _ in self.gateway.calls])

    def test_test_period_records_live_schema_evidence(self):
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        scope = agent._scope("fundamental forecast")
        self.assertEqual(scope["test_period"], "P0Y0M")
        self.assertEqual(scope["_test_period_evidence"]["minValue"], "P0Y0M0D")
        self.assertEqual(scope["_test_period_evidence"]["maxValue"], "P6Y0M0D")

    def test_invalid_expression_blocks_simulation(self):
        self.model.decisions["construct_batch"]["variants"][0]["expression"] = "rank(unknown_field)"
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertNotIn("simulate_batch", [name for name, _ in self.gateway.calls])

    def test_field_without_dataset_identity_is_not_admitted(self):
        self.gateway.omit_field_dataset = True
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertNotIn("simulate_batch", [name for name, _ in self.gateway.calls])

    def test_field_without_definition_is_not_admitted(self):
        self.gateway.omit_field_description = True
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertNotIn("simulate_batch", [name for name, _ in self.gateway.calls])

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
                if name == "simulate_batch"]
        self.assertEqual(len(sims), 2)
        self.assertEqual(sims[0]["settings"]["neutralization"], "NONE")
        self.assertEqual(sims[1]["settings"]["neutralization"], "MARKET")
        self.assertEqual(result["alpha_ids"], ["A1", "A2"])
        self.assertEqual(len(self.model.contexts["construct_batch"]["probe_results"]), 2)
        self.assertNotIn("P1", result["alpha_ids"])
        batch = next(Path(self.tmp.name).rglob("metering.json"))
        self.assertEqual(json.loads(batch.read_text(encoding="utf-8"))["alpha_expressions"], 4)
        context = json.loads((batch.parent / "batch_context.json").read_text(encoding="utf-8"))
        intent = json.loads((batch.parent / "probe_1_intent.json").read_text(encoding="utf-8"))
        self.assertEqual(intent["runner"]["case_key"], context["platform_case_key"])
        self.assertEqual(intent["runner"]["run_id"], context["platform_run_id"])

    def test_probe_settings_must_be_live_supported(self):
        self.gateway.allow_probe_neutralization = False
        self.model.decisions["plan_probes"] = {"core_fields": ["signal_a"],
            "probes": [{"field_id": "signal_a", "expression": "signal_a",
                        "question": "nominal coverage", "measures": ["nominal_coverage"],
                        "economic_relevance": "stock pool", "expected": "broad",
                        "failure_criterion": "narrow"}]}
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertNotIn("simulate_batch", [name for name, _ in self.gateway.calls])

    def test_missing_region_blocks_before_platform_access(self):
        self.config["scope"]["region"] = None
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(self.gateway.calls, [])

    def test_invalid_gate_threshold_is_blocked_before_platform_calls(self):
        self.config["gate"]["prod_corr_max"] = 1.01
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(self.gateway.calls, [])

    def test_per_batch_auth_mode_cannot_silently_run(self):
        self.config["auth"]["mode"] = "per_batch"
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(self.gateway.calls, [])

    def test_lost_response_without_runner_provenance_remains_unresolved(self):
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
        # Platform records carry settings in the platform's own
        # (camelCase) key shape, not the intent's snake_case shape.
        platform_keys = {"instrument_type": "instrumentType",
                         "unit_handling": "unitHandling",
                         "nan_handling": "nanHandling",
                         "test_period": "testPeriod",
                         "max_trade": "maxTrade"}
        platform_settings = {platform_keys.get(k, k): v
                             for k, v in intent["settings"].items()}
        self.gateway.recent_alphas = [
            {"id": "A1", "regular": {"code": "rank(signal_a)"},
             "settings": platform_settings},
            {"id": "A2", "regular": {"code": "rank(signal_b)"},
             "settings": platform_settings},
        ]
        with self.assertRaisesRegex(ResearchError, "provenance"):
            agent.recover(batch)
        recovery = json.loads((batch / "recovery.json").read_text(encoding="utf-8"))
        self.assertEqual(recovery["status"], "UNRESOLVED_PROVENANCE")
        self.assertFalse((batch / "results.json").exists())
        self.assertEqual(sum(name == "simulate_batch"
                             for name, _ in self.gateway.calls), 1)

    def test_ra_failure_never_reaches_correlation(self):
        self.gateway.ra_result = "FAIL"
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        result = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertNotIn("check_correlation", [name for name, _ in self.gateway.calls])
        self.assertNotIn("check_local_self_correlation", [name for name, _ in self.gateway.calls])
        self.assertNotIn("get_submission_check", [name for name, _ in self.gateway.calls])
        self.assertEqual(result["gate"]["A1"]["status"], "RA_FAILED")

    def test_local_self_breach_blocks_platform_correlation_and_candidate(self):
        self.gateway.single_ids = ["A1"]
        self.gateway.full_details = True
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "level baseline"}]
        self.gateway.local_self_by_alpha["A1"] = {
            "status": "fail", "value": 0.9, "complete": True,
            "comparison_count": 1, "window_years": 4, "limit": 0.7}
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        result = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertNotIn("check_correlation", [name for name, _ in self.gateway.calls])
        self.assertIn("local_self_correlation", result["gate"]["A1"]["failures"])
        self.assertEqual(result["gate"]["A1"]["status"], "CORR_REJECTED")

    def test_local_self_unavailable_blocks_platform_correlation_and_stays_pending(self):
        self.gateway.single_ids = ["A1"]
        self.gateway.full_details = True
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "level baseline"}]
        self.gateway.local_self_by_alpha["A1"] = {"status": "pending", "value": None,
                                                  "complete": False, "limit": 0.7}
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        result = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertNotIn("check_correlation", [name for name, _ in self.gateway.calls])
        self.assertIn("local_self_correlation_unavailable", result["gate"]["A1"]["pending"])
        self.assertNotEqual(result["gate"]["A1"]["status"], "CANDIDATE")

    def test_local_self_rejected_alpha_is_not_in_pairwise_review_pool(self):
        self.gateway.full_details = True
        self.gateway.local_self_by_alpha["A2"] = {
            "status": "fail", "value": 0.9, "complete": True,
            "comparison_count": 1, "window_years": 4, "limit": 0.7}
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        result = agent.run_once("slow lending signal", dataset_id="risk60")
        platform_calls = [params for name, params in self.gateway.calls if name == "check_correlation"]
        self.assertEqual([row["alpha_id"] for row in platform_calls], ["A1"])
        self.assertNotIn("A2", platform_calls[0]["contender_ids"])
        audit = next(params for name, params in self.gateway.calls if name == "audit_alphas")
        self.assertNotIn("A2", audit["contender_ids"])
        self.assertIn("local_self_correlation", result["gate"]["A2"]["failures"])

    def test_ra_failure_discovered_by_prefilter_excludes_new_contender(self):
        self.gateway.full_details = True
        latest = {"id": "A1", "is": {"sharpe": 1.8, "fitness": 1.2, "sharpe_2y": 1.7,
                  "margin": 0.001, "turnover": 0.2,
                  "checks": [{"name": "LOW_SHARPE", "result": "FAIL"}]}}
        self.gateway.local_self_by_alpha["A1"] = {
            "status": "pending", "ra_state": "fail", "ra_details": latest, "limit": 0.7}
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))

        result = agent.run_once("slow lending signal", dataset_id="risk60")

        self.assertIn("ra:LOW_SHARPE", result["gate"]["A1"]["failures"])
        calls = [params for name, params in self.gateway.calls
                 if name == "check_correlation" and params["alpha_id"] == "A2"]
        self.assertNotIn("A1", calls[-1]["contender_ids"])

    def test_ra_pending_discovered_by_prefilter_keeps_candidate_pending(self):
        self.gateway.single_ids = ["A1"]
        self.gateway.full_details = True
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "baseline"}]
        latest = {"id": "A1", "is": {"sharpe": 1.8, "fitness": 1.2, "sharpe_2y": 1.7,
                  "margin": 0.001, "turnover": 0.2,
                  "checks": [{"name": "LOW_SHARPE", "result": "PENDING"}]}}
        self.gateway.local_self_by_alpha["A1"] = {
            "status": "pass", "value": 0.3, "complete": True,
            "comparison_count": 1, "window_years": 4, "limit": 0.7,
            "ra_state": "pending", "ra_details": latest}
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))

        result = agent.run_once("slow lending signal", dataset_id="risk60")

        self.assertIn("check_correlation", [name for name, _ in self.gateway.calls])
        self.assertIn("ra_pending:LOW_SHARPE", result["gate"]["A1"]["pending"])
        self.assertNotEqual(result["gate"]["A1"]["status"], "CANDIDATE")

    def test_zero_ra_failures_record_raw_correlation_evidence(self):
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        result = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(sum(name == "check_correlation"
                             for name, _ in self.gateway.calls), 2)
        self.assertEqual(result["gate"]["A1"]["correlations"]["prod"], 0.5)

    def test_ra_eligible_alpha_remains_in_pairwise_pool_despite_metric_failure(self):
        self.gateway.full_details = True
        self.gateway.sharpe_by_alpha["A2"] = 1.0
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))

        result = agent.run_once("slow lending signal", dataset_id="risk60")

        self.assertIn("sharpe", result["gate"]["A2"]["failures"])
        calls = {params["alpha_id"]: params for name, params in self.gateway.calls
                 if name == "check_correlation"}
        self.assertIn("A2", calls["A1"]["contender_ids"])
        self.assertIn("A1", calls["A2"]["contender_ids"])

    def test_stop_flag_blocks_new_simulation(self):
        stop_flag = Path(self.tmp.name) / "stop.flag"
        stop_flag.write_text("stop\n", encoding="utf-8")
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name), stop_flag=stop_flag)
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertNotIn("simulate_batch", [name for name, _ in self.gateway.calls])

    def test_stopped_before_gateway_can_resume_locked_production_batch(self):
        stop_flag = Path(self.tmp.name) / "stop.flag"
        stop_flag.write_text("stop\n", encoding="utf-8")
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name), stop_flag=stop_flag)
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        batch = agent._unfinished_batch()
        intent = json.loads((batch / "simulation_intent.json").read_text(encoding="utf-8"))
        self.assertEqual(intent["status"], "STOPPED_BEFORE_SUBMISSION")
        self.assertNotIn("simulate_batch", [name for name, _ in self.gateway.calls])

        stop_flag.unlink()
        recovered = agent.recover(batch)

        self.assertEqual(recovered["alpha_ids"], ["A1", "A2"])
        self.assertEqual(sum(name == "simulate_batch" for name, _ in self.gateway.calls), 1)

    def test_stopped_before_gateway_can_resume_diagnostic_probe(self):
        self.gateway.catalog_incomplete = True
        self.gateway.single_ids = ["P1", "A1"]
        self.gateway.single_probe_counts = True
        self.model.decisions["plan_probes"] = {
            "core_fields": ["signal_a"],
            "required_dimensions": {"signal_a": ["nominal_coverage", "usable_observations"]},
            "requirement_reason": "coverage matters",
            "probes": [{"field_id": "signal_a", "expression": "signal_a != 0 ? 1 : 0",
                        "question": "usable observations", "measures": ["usable_observations"],
                        "economic_relevance": "avoid zero-filled rank", "expected": "broad",
                        "failure_criterion": "few stocks"}]}
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "baseline"}]
        self.model.decisions["construct_batch"]["field_report"] = [{
            "field_id": "signal_a", "verdict": "ADMIT", "constraints": ["approximate count"],
            "evidence": {"nominal_coverage": {"source": "catalog", "observation": "0.9"},
                         "usable_observations": {"source": "probe:0", "observation": "120/100"}},
            "unresolved": {key: {"reason": "unavailable", "why_not_material": "level rank"}
                           for key in ("refresh_rate", "scale_units", "typical_level", "distribution")}}]
        stop_flag = Path(self.tmp.name) / "stop.flag"
        stop_flag.write_text("stop\n", encoding="utf-8")
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name), stop_flag=stop_flag)
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        batch = agent._unfinished_batch()
        intent_path = batch / "probe_1_intent.json"
        self.assertEqual(json.loads(intent_path.read_text(encoding="utf-8"))["status"],
                         "STOPPED_BEFORE_SUBMISSION")
        self.assertNotIn("simulate_batch", [name for name, _ in self.gateway.calls])

        stop_flag.unlink()
        result = agent.recover(batch)

        self.assertEqual(result["alpha_ids"], ["A1"])
        self.assertEqual(json.loads(intent_path.read_text(encoding="utf-8"))["status"], "READ_BACK")
        self.assertEqual(sum(name == "simulate_batch" for name, _ in self.gateway.calls), 2)

    def test_missing_hypothesis_direction_blocks_alpha_batch(self):
        del self.model.decisions["construct_batch"]["expected_sign"]
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertNotIn("simulate_batch", [name for name, _ in self.gateway.calls])

    def test_incomplete_catalog_without_probes_blocks_construction(self):
        self.gateway.catalog_incomplete = True
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertNotIn("construct_batch", self.model.tasks)

    def test_probe_alpha_id_alone_does_not_prove_field_properties(self):
        self.gateway.catalog_incomplete = True
        self.gateway.single_ids = ["P1"]
        self.model.decisions["plan_probes"] = {"core_fields": ["signal_a"],
            "probes": [{"field_id": "signal_a", "expression": "signal_a",
                        "question": "field distribution",
                        "measures": ["usable_observations", "refresh_rate",
                                     "scale_units", "typical_level", "distribution"],
                        "economic_relevance": "slow signal", "expected": "stable",
                        "failure_criterion": "unknown"}]}
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertNotIn("construct_batch", self.model.tasks)

    def test_nonmaterial_unknown_field_properties_are_explicitly_recorded(self):
        self.gateway.catalog_incomplete = True
        self.gateway.single_ids = ["P1", "A1"]
        self.gateway.single_probe_counts = True
        self.model.decisions["plan_probes"] = {
            "core_fields": ["signal_a"],
            "required_dimensions": {"signal_a": ["nominal_coverage",
                                                  "usable_observations"]},
            "requirement_reason": "coverage matters for cross-sectional breadth",
            "probes": [{"field_id": "signal_a",
                        "expression": "signal_a != 0 ? 1 : 0",
                        "question": "usable observations",
                        "measures": ["usable_observations"],
                        "economic_relevance": "avoid zero-filled rank",
                        "expected": "broad", "failure_criterion": "few stocks"}]}
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "level baseline"}]
        self.model.decisions["construct_batch"]["field_report"] = [{
            "field_id": "signal_a", "verdict": "ADMIT",
            "constraints": ["treat coverage count as approximate"],
            "evidence": {
                "nominal_coverage": {"source": "catalog", "observation": "0.9"},
                "usable_observations": {"source": "probe:0", "observation": "120/100"}},
            "unresolved": {key: {"reason": "not available in catalog",
                                 "why_not_material": "level rank does not assume this property"}
                           for key in ("refresh_rate", "scale_units",
                                       "typical_level", "distribution")}
        }]
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        result = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(result["alpha_ids"], ["A1"])

    def test_missing_universe_is_selected_from_live_options(self):
        self.config["scope"]["universe"] = None
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        result = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(result["dataset_id"], "risk60")
        self.assertIn("select_scope", self.model.tasks)

    def test_model_receives_only_regular_scope_operators(self):
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        agent.run_once("slow lending signal", dataset_id="risk60")
        names = {row["name"] for row in self.model.contexts["construct_batch"]["operator_catalog"]}
        self.assertIn("rank", names)
        self.assertNotIn("combo_a", names)

    def test_invalid_scope_choice_blocks_before_dataset_request(self):
        self.config["scope"]["universe"] = None
        self.model.decisions["select_scope"]["universe"] = "MADE_UP"
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertNotIn("get_datasets", [name for name, _ in self.gateway.calls])

    def test_autonomous_dataset_choice_requires_saved_reason(self):
        self.model.decisions["select_dataset"] = {"dataset_id": "risk60"}
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal")
        self.assertNotIn("simulate_batch", [name for name, _ in self.gateway.calls])

    def test_autonomous_dataset_choice_reason_is_in_preregistration(self):
        self.model.decisions["select_dataset"] = {
            "dataset_id": "risk60", "reason": "slow coverage relates to lender risk"}
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        agent.run_once("slow lending signal")
        prereg = json.loads(next(Path(self.tmp.name).rglob("prereg.json")).read_text(
            encoding="utf-8"))
        self.assertEqual(prereg["dataset_selection"]["mode"], "autonomous")
        self.assertIn("lender risk", prereg["dataset_selection"]["reason"])

    def test_configured_dataset_is_kept_exact(self):
        self.config["scope"]["dataset_id"] = "risk60"
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="other")
        self.assertNotIn("get_datasets", [name for name, _ in self.gateway.calls])
        result = agent.run_once("slow lending signal")
        self.assertEqual(result["dataset_id"], "risk60")
        self.assertNotIn("select_dataset", self.model.tasks)

    def test_interrupted_probe_without_provenance_stays_unresolved(self):
        self.gateway.fail_probe_once = True
        self.model.decisions["plan_probes"] = {"core_fields": ["signal_a", "signal_b"],
            "probes": [
                {"field_id": "signal_a", "expression": "signal_a",
                 "question": "nominal coverage", "measures": ["nominal_coverage"],
                 "economic_relevance": "stock pool", "expected": "broad",
                 "failure_criterion": "narrow"},
                {"field_id": "signal_b", "expression": "signal_b",
                 "question": "nominal coverage", "measures": ["nominal_coverage"],
                 "economic_relevance": "stock pool", "expected": "broad",
                 "failure_criterion": "narrow"},
            ]}
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(RuntimeError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        batch = next(Path(self.tmp.name).rglob("probe_1_intent.json")).parent
        intent = json.loads((batch / "probe_1_intent.json").read_text(encoding="utf-8"))
        self.gateway.recent_alphas = [
            {"id": "P1", "regular": {"code": "signal_a"},
             "settings": intent["settings"]},
            {"id": "P2", "regular": {"code": "signal_b"},
             "settings": intent["settings"]},
        ]
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        with self.assertRaisesRegex(ResearchError, "provenance"):
            agent.recover(batch)
        recovery = json.loads((batch / "probe_1_recovery.json").read_text(encoding="utf-8"))
        self.assertEqual(recovery["status"], "UNRESOLVED_PROVENANCE")
        self.assertNotIn("construct_batch", self.model.tasks)
        self.assertEqual(sum(name == "simulate_batch"
                             for name, _ in self.gateway.calls), 1)

    def test_recover_before_probe_plan_without_abandoning_batch(self):
        self.model.fail_task_once = "plan_probes"
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(RuntimeError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        batch = next(Path(self.tmp.name).rglob("batch_context.json")).parent
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        result = agent.recover(batch)
        self.assertEqual(result["alpha_ids"], ["A1", "A2"])
        self.assertEqual(sum(name == "simulate_batch"
                             for name, _ in self.gateway.calls), 1)

    def test_recover_after_simulation_response_uses_saved_children(self):
        self.model.fail_task_once = "attribute_results"
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(RuntimeError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        batch = next(Path(self.tmp.name).rglob("simulation_raw.json")).parent
        result = agent.recover(batch)
        self.assertEqual(result["alpha_ids"], ["A1", "A2"])
        self.assertNotIn("get_user_alphas", [name for name, _ in self.gateway.calls])
        self.assertEqual(sum(name == "simulate_batch"
                             for name, _ in self.gateway.calls), 1)

    def test_invalid_draft_can_be_voided_without_erasing_evidence(self):
        self.model.decisions["construct_batch"]["variants"][0]["expression"] = "bad_field"
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        batch = next(Path(self.tmp.name).rglob("batch_context.json")).parent
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        agent.void_batch(batch, "invalid model proposal")
        self.assertTrue((batch / "batch_context.json").exists())
        self.assertTrue((batch / "void.json").exists())
        self.model.decisions["construct_batch"]["variants"][0]["expression"] = "rank(signal_a)"
        result = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(result["alpha_ids"], ["A1", "A2"])

    def test_submitted_batch_cannot_be_voided_without_reconciliation(self):
        self.gateway.fail_simulation = True
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(RuntimeError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        batch = next(Path(self.tmp.name).rglob("simulation_intent.json")).parent
        with self.assertRaises(ResearchError):
            agent.void_batch(batch, "try to skip")

    def test_completed_diagnostics_can_be_archived_if_alpha_draft_is_invalid(self):
        self.gateway.single_ids = ["P1"]
        self.model.decisions["plan_probes"] = {"core_fields": ["signal_a"],
            "probes": [{"field_id": "signal_a", "expression": "signal_a",
                        "question": "nominal coverage", "measures": ["nominal_coverage"],
                        "economic_relevance": "stock pool", "expected": "broad",
                        "failure_criterion": "narrow"}]}
        self.model.decisions["construct_batch"]["variants"][0]["expression"] = "bad_field"
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        batch = next(Path(self.tmp.name).rglob("probe_1_intent.json")).parent
        agent.void_batch(batch, "invalid alpha draft")
        self.assertTrue((batch / "void.json").exists())

    def test_full_gate_candidate_requires_model_review(self):
        self.gateway.single_ids = ["A1"]
        self.gateway.full_details = True
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "level baseline"}]
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        result = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(result["gate"]["A1"]["status"], "CANDIDATE")
        self.assertEqual(result["candidate_status"], "CANDIDATE")
        self.assertIn("review_candidate", self.model.tasks)

    def test_unresolved_robustness_review_never_counts_candidate(self):
        self.gateway.single_ids = ["A1"]
        self.gateway.full_details = True
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "level baseline"}]
        self.model.decisions["review_candidate"] = {
            "verdict": "ROBUSTNESS_REVIEW", "reason": "unstable annual return",
            "evidence_refs": ["readback"]}
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        result = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(result["gate"]["A1"]["status"], "ROBUSTNESS_REVIEW")
        self.assertEqual(result["candidate_status"], "PENDING_FULL_GATE")

    def test_pairwise_conflict_keeps_existing_and_new_contenders_pending(self):
        self.gateway.single_ids = ["A1", "A2"]
        self.gateway.full_details = True
        self.gateway.correlation_by_alpha = {
            "A1": [["OTHER", 0.3]], "A2": [["A1", 0.45]]}
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "level baseline"}]
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        first = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(first["gate"]["A1"]["status"], "CANDIDATE")
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_b)", "reason": "independent field control"}]
        second = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(second["gate"]["A2"]["status"], "PAIRWISE_CONFLICT")
        case = next((Path(self.tmp.name) / "cases").glob("*.json"))
        state = json.loads(case.read_text(encoding="utf-8"))
        self.assertEqual(state["contenders"]["A1"]["status"], "PAIRWISE_CONFLICT")
        self.assertEqual(state["contenders"]["A2"]["status"], "PAIRWISE_CONFLICT")
        self.assertEqual(second["candidate_status"], "PENDING_FULL_GATE")

    def test_retained_contender_ra_is_rechecked_before_new_pairwise_comparison(self):
        self.gateway.single_ids = ["A1", "A2"]
        self.gateway.full_details = True
        self.gateway.correlation_by_alpha = {"A1": [["OTHER", 0.3]], "A2": [["A1", 0.45]]}
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "level baseline"}]
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        agent.run_once("slow lending signal", dataset_id="risk60")
        self.gateway.ra_by_alpha["A1"] = "FAIL"
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_b)", "reason": "independent control"}]

        second = agent.run_once("slow lending signal", dataset_id="risk60")

        latest = [params for name, params in self.gateway.calls
                  if name == "check_correlation" and params["alpha_id"] == "A2"][-1]
        self.assertNotIn("A1", latest["contender_ids"])
        self.assertNotEqual(second["gate"]["A2"]["status"], "PAIRWISE_CONFLICT")
        case = next((Path(self.tmp.name) / "cases").glob("*.json"))
        self.assertEqual(json.loads(case.read_text(encoding="utf-8"))["contenders"]["A1"]["status"], "RA_FAILED")
        states = json.loads((Path(self.tmp.name) / "alpha_states.json").read_text(encoding="utf-8"))
        self.assertEqual(states["A1"]["state"], "RA_FAILED")
        self.assertEqual(states["A1"]["ra_fail_count"], 1)

    def test_retained_candidate_failing_fresh_metrics_is_not_counted(self):
        self.gateway.single_ids = ["A1", "A2"]
        self.gateway.full_details = True
        self.gateway.correlation_by_alpha = {"A1": [["OTHER", 0.3]], "A2": [["A1", 0.2]]}
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "baseline"}]
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        first = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(first["gate"]["A1"]["status"], "CANDIDATE")
        self.gateway.sharpe_by_alpha["A1"] = 1.0
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_b)", "reason": "independent control"}]

        second = agent.run_once("slow lending signal", dataset_id="risk60")

        case = json.loads(Path(second["case_path"]).read_text(encoding="utf-8"))
        old = case["contenders"]["A1"]
        self.assertNotEqual(old["status"], "CANDIDATE")
        evidence = json.loads((Path(second["batch_dir"]) / "retained_candidate_audits.json")
                              .read_text(encoding="utf-8"))
        self.assertIn("sharpe", evidence["A1"]["failures"])

    def test_retained_candidate_failing_fresh_production_correlation_is_not_counted(self):
        self.gateway.single_ids = ["A1", "A2"]
        self.gateway.full_details = True
        self.gateway.correlation_by_alpha = {"A1": [["OTHER", 0.3]], "A2": [["A1", 0.2]]}
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "baseline"}]
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        self.assertEqual(agent.run_once("slow lending signal", dataset_id="risk60")["gate"]["A1"]["status"], "CANDIDATE")
        self.gateway.prod_by_alpha["A1"] = 0.85
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_b)", "reason": "independent control"}]

        second = agent.run_once("slow lending signal", dataset_id="risk60")

        case = json.loads(Path(second["case_path"]).read_text(encoding="utf-8"))
        self.assertNotEqual(case["contenders"]["A1"]["status"], "CANDIDATE")
        evidence = json.loads((Path(second["batch_dir"]) / "retained_candidate_audits.json")
                              .read_text(encoding="utf-8"))
        self.assertIn("prod_correlation", evidence["A1"]["failures"])

    def test_retained_reaudit_preserves_known_metric_failure_when_correlation_unavailable(self):
        self.gateway.single_ids = ["A1", "A2"]
        self.gateway.full_details = True
        self.gateway.correlation_by_alpha = {"A1": [["OTHER", 0.3]], "A2": [["A1", 0.2]]}
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "baseline"}]
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        agent.run_once("slow lending signal", dataset_id="risk60")
        self.gateway.sharpe_by_alpha["A1"] = 1.0
        self.gateway.correlation_unavailable.add("A1")
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_b)", "reason": "independent control"}]

        second = agent.run_once("slow lending signal", dataset_id="risk60")

        evidence = json.loads((Path(second["batch_dir"]) / "retained_candidate_audits.json")
                              .read_text(encoding="utf-8"))["A1"]
        self.assertIn("sharpe", evidence["failures"])
        self.assertTrue(any(item.startswith("retained_reaudit_unavailable:")
                            for item in evidence["pending"]))

    def test_retained_candidate_stays_candidate_only_after_complete_reaudit(self):
        self.gateway.single_ids = ["A1", "A2"]
        self.gateway.full_details = True
        self.gateway.correlation_by_alpha = {"A1": [["OTHER", 0.3]], "A2": [["A1", 0.2]]}
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "baseline"}]
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        self.assertEqual(agent.run_once("slow lending signal", dataset_id="risk60")["gate"]["A1"]["status"], "CANDIDATE")
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_b)", "reason": "independent control"}]

        second = agent.run_once("slow lending signal", dataset_id="risk60")

        case = json.loads(Path(second["case_path"]).read_text(encoding="utf-8"))
        self.assertEqual(case["contenders"]["A1"]["status"], "CANDIDATE")
        evidence = json.loads((Path(second["batch_dir"]) / "retained_candidate_audits.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual(evidence["A1"]["status"], "CANDIDATE")
        self.assertTrue(any(name == "audit_alphas" and params.get("audit_id", "").endswith("_A1")
                            for name, params in self.gateway.calls))

    def test_retained_candidate_with_changed_preregistration_is_pending(self):
        self.gateway.single_ids = ["A1", "A2"]
        self.gateway.full_details = True
        self.gateway.correlation_by_alpha = {"A1": [["OTHER", 0.3]], "A2": [["A1", 0.2]]}
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "baseline"}]
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        first = agent.run_once("slow lending signal", dataset_id="risk60")
        prior_prereg = Path(first["batch_dir"]) / "prereg.json"
        prior_prereg.write_text(prior_prereg.read_text(encoding="utf-8") + " ", encoding="utf-8")
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_b)", "reason": "independent control"}]

        second = agent.run_once("slow lending signal", dataset_id="risk60")

        case = json.loads(Path(second["case_path"]).read_text(encoding="utf-8"))
        self.assertEqual(case["contenders"]["A1"]["status"], "PENDING_FULL_AUDIT")
        evidence = json.loads((Path(second["batch_dir"]) / "retained_candidate_audits.json")
                              .read_text(encoding="utf-8"))
        self.assertTrue(any(item.startswith("retained_reaudit_unavailable:")
                            for item in evidence["A1"]["pending"]))

    def test_interrupted_retained_reaudit_does_not_leave_stale_candidate(self):
        self.gateway.single_ids = ["A1", "A2"]
        self.gateway.full_details = True
        self.gateway.correlation_by_alpha = {"A1": [["OTHER", 0.3]], "A2": [["A1", 0.2]]}
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "baseline"}]
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        first = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(first["gate"]["A1"]["status"], "CANDIDATE")
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_b)", "reason": "independent control"}]
        original = agent._refresh_retained_contenders
        def interrupted(*args, **kwargs):
            raise RuntimeError("reaudit interrupted")
        agent._refresh_retained_contenders = interrupted

        with self.assertRaisesRegex(RuntimeError, "reaudit interrupted"):
            agent.run_once("slow lending signal", dataset_id="risk60")

        case = json.loads(Path(first["case_path"]).read_text(encoding="utf-8"))
        self.assertNotEqual(case["contenders"]["A1"]["status"], "CANDIDATE")
        agent._refresh_retained_contenders = original
        recovered = agent.recover(agent._unfinished_batch())
        case = json.loads(Path(recovered["case_path"]).read_text(encoding="utf-8"))
        self.assertEqual(case["contenders"]["A1"]["status"], "CANDIDATE")
        self.assertEqual(sum(name == "simulate_batch" for name, _ in self.gateway.calls), 2)

    def test_retained_pairwise_conflict_demotes_both_old_candidates(self):
        self.gateway.full_details = True
        self.gateway.correlation_by_alpha = {"A1": [["A2", 0.2]], "A2": [["A1", 0.2]]}
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        first = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(first["gate"]["A1"]["status"], "CANDIDATE")
        self.assertEqual(first["gate"]["A2"]["status"], "CANDIDATE")
        self.gateway.single_ids = ["A3"]
        self.gateway.correlation_by_alpha = {
            "A1": [["A2", 0.55]], "A2": [["A1", 0.2]],
            "A3": [["A1", 0.2], ["A2", 0.2]]}
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(ts_mean(signal_a, 5))", "reason": "slow horizon control"}]

        second = agent.run_once("slow lending signal", dataset_id="risk60")

        case = json.loads(Path(second["case_path"]).read_text(encoding="utf-8"))
        self.assertEqual(case["contenders"]["A1"]["status"], "PAIRWISE_CONFLICT")
        self.assertEqual(case["contenders"]["A2"]["status"], "PAIRWISE_CONFLICT")

    def test_unreadable_retained_contender_blocks_new_candidate_verdict(self):
        self.gateway.single_ids = ["A1", "A2"]
        self.gateway.full_details = True
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "level baseline"}]
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        agent.run_once("slow lending signal", dataset_id="risk60")
        self.gateway.unavailable_details.add("A1")
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_b)", "reason": "independent control"}]

        second = agent.run_once("slow lending signal", dataset_id="risk60")

        self.assertNotEqual(second["gate"]["A2"]["status"], "CANDIDATE")
        self.assertIn("retained_contender_unverified:A1", second["gate"]["A2"]["pending"])
        states = json.loads((Path(self.tmp.name) / "alpha_states.json").read_text(encoding="utf-8"))
        self.assertFalse(states["A1"]["ra_readable"])

    def test_retained_contender_with_changed_scope_is_not_silently_pooled(self):
        self.gateway.single_ids = ["A1", "A2"]
        self.gateway.full_details = True
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "level baseline"}]
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        agent.run_once("slow lending signal", dataset_id="risk60")
        self.gateway.settings_by_alpha["A1"] = {"universe": "OTHER"}
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_b)", "reason": "independent control"}]

        second = agent.run_once("slow lending signal", dataset_id="risk60")

        calls = [params for name, params in self.gateway.calls
                 if name == "check_correlation" and params["alpha_id"] == "A2"]
        self.assertNotIn("A1", calls[-1]["contender_ids"])
        self.assertIn("retained_contender_unverified:A1", second["gate"]["A2"]["pending"])
        evidence = json.loads((Path(second["batch_dir"]) / "retained_correlation_precheck.json")
                              .read_text(encoding="utf-8"))
        self.assertIn("details", evidence["A1"])
        self.assertEqual(evidence["A1"]["details"]["settings"]["universe"], "OTHER")

    def test_retained_contender_local_self_is_rechecked_before_pairwise(self):
        self.gateway.single_ids = ["A1", "A2"]
        self.gateway.full_details = True
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "level baseline"}]
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        agent.run_once("slow lending signal", dataset_id="risk60")
        self.gateway.local_self_by_alpha["A1"] = {
            "status": "fail", "value": 0.9, "complete": True,
            "comparison_count": 1, "window_years": 4, "limit": 0.7}
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_b)", "reason": "independent control"}]

        agent.run_once("slow lending signal", dataset_id="risk60")

        latest = [params for name, params in self.gateway.calls
                  if name == "check_correlation" and params["alpha_id"] == "A2"][-1]
        self.assertNotIn("A1", latest["contender_ids"])
        case = next((Path(self.tmp.name) / "cases").glob("*.json"))
        saved = json.loads(case.read_text(encoding="utf-8"))["contenders"]["A1"]
        self.assertTrue(saved["local_self_rejected"])
        self.assertEqual(saved["status"], "CORR_REJECTED")

    def test_retained_contender_new_ra_failure_during_prefilter_excludes_it(self):
        self.gateway.single_ids = ["A1", "A2"]
        self.gateway.full_details = True
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "level baseline"}]
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        agent.run_once("slow lending signal", dataset_id="risk60")
        latest = self.gateway._details("A1")
        latest["is"]["checks"][0]["result"] = "FAIL"
        self.gateway.local_self_by_alpha["A1"] = {
            "status": "pending", "ra_state": "fail", "ra_details": latest, "limit": 0.7}
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_b)", "reason": "independent control"}]

        second = agent.run_once("slow lending signal", dataset_id="risk60")

        calls = [params for name, params in self.gateway.calls
                 if name == "check_correlation" and params["alpha_id"] == "A2"]
        self.assertNotIn("A1", calls[-1]["contender_ids"])
        evidence = json.loads((Path(second["batch_dir"]) / "retained_correlation_precheck.json")
                              .read_text(encoding="utf-8"))
        self.assertEqual(evidence["A1"]["status"], "ra_failed")
        self.assertEqual(evidence["A1"]["prefilter_details"]["is"]["checks"][0]["result"], "FAIL")

    def test_existing_pairwise_conflict_is_not_erased_by_a_later_ra_failure(self):
        self.gateway.full_details = True
        self.gateway.correlation_by_alpha = {"A1": [["OTHER", 0.3]], "A2": [["A1", 0.45]]}
        agent = ResearchAgent(self.gateway, self.model, self.config, Path(self.tmp.name))
        result = agent.run_once("slow lending signal", dataset_id="risk60")
        case_path = next((Path(self.tmp.name) / "cases").glob("*.json"))
        case = json.loads(case_path.read_text(encoding="utf-8"))
        self.assertEqual(case["contenders"]["A1"]["status"], "PAIRWISE_CONFLICT")
        self.gateway.ra_by_alpha["A1"] = "FAIL"

        agent._refresh_retained_contenders(case["contenders"], [], case["scope"], 0.7,
                                           Path(result["batch_dir"]))

        self.assertTrue(case["contenders"]["A1"]["ra_rejected"])
        self.assertEqual(case["contenders"]["A1"]["status"], "PAIRWISE_CONFLICT")

    def test_pairwise_conflict_within_one_batch_demotes_both(self):
        self.gateway.full_details = True
        self.gateway.correlation_by_alpha = {
            "A1": [["OTHER", 0.3]], "A2": [["A1", 0.45]]}
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        result = agent.run_once("slow lending signal", dataset_id="risk60")
        self.assertEqual(result["gate"]["A1"]["status"], "PAIRWISE_CONFLICT")
        self.assertEqual(result["gate"]["A2"]["status"], "PAIRWISE_CONFLICT")
        self.assertEqual(result["candidate_status"], "PENDING_FULL_GATE")

    def test_same_case_does_not_backtest_an_expression_again(self):
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        agent.run_once("slow lending signal", dataset_id="risk60")
        before = sum(name == "simulate_batch" for name, _ in self.gateway.calls)
        with self.assertRaises(ResearchError):
            agent.run_once("slow lending signal", dataset_id="risk60")
        after = sum(name == "simulate_batch" for name, _ in self.gateway.calls)
        self.assertEqual(before, after)

    def test_completed_expressions_are_cached_across_objectives_and_restarts(self):
        runs_root = Path(self.tmp.name) / "runs"
        agent = ResearchAgent(self.gateway, self.model, self.config, runs_root)
        agent.run_once("slow lending signal", dataset_id="risk60")
        simulation_count = sum(name == "simulate_batch"
                               for name, _ in self.gateway.calls)
        cache = getattr(agent, "backtest_cache", None)
        self.assertIsNotNone(cache)
        cache_path = cache.path
        self.assertTrue(cache_path.exists())

        # A new process uses the persisted index.
        restarted = ResearchAgent(self.gateway, self.model, self.config, runs_root)
        with self.assertRaisesRegex(ResearchError, "backtest cache hit"):
            restarted.run_once("different lending objective", dataset_id="risk60")
        self.assertEqual(sum(name == "simulate_batch"
                             for name, _ in self.gateway.calls), simulation_count)

        # If the index is lost, closed run artifacts rebuild it before a new run.
        cache_path.unlink()
        rebuilt = ResearchAgent(self.gateway, self.model, self.config, runs_root)
        with self.assertRaisesRegex(ResearchError, "backtest cache hit"):
            rebuilt.run_once("third lending objective", dataset_id="risk60")
        self.assertEqual(sum(name == "simulate_batch"
                             for name, _ in self.gateway.calls), simulation_count)

    def test_bloom_maybe_hit_is_cached_and_keys_include_all_settings(self):
        runs_root = Path(self.tmp.name) / "cache-runs"
        agent = ResearchAgent(self.gateway, self.model, self.config, runs_root)
        cache = getattr(agent, "backtest_cache", None)
        self.assertIsNotNone(cache)
        scope = {key: self.config["scope"][key]
                 for key in ("region", "universe", "delay")}
        settings = agent._platform_settings(self.config["scope"])
        expression = "rank(signal_a)"
        self.assertIsNone(cache.find(expression, "risk60", scope, settings))
        cache.record(expression, "risk60", scope, settings)
        self.assertTrue(cache.find(expression, "risk60", scope, settings))
        changed_settings = {**settings, "truncation": 0.09}
        self.assertIsNone(cache.find(expression, "risk60", scope, changed_settings))
        self.assertIsNone(cache.find(expression, "other-dataset", scope, settings))
        changed_scope = {**scope, "universe": "TOP1000"}
        self.assertIsNone(cache.find(expression, "risk60", changed_scope, settings))

    def test_bounded_run_advances_two_distinct_batches(self):
        self.gateway.single_ids = ["A1", "A2"]
        first = deepcopy(self.model.decisions["construct_batch"])
        second = deepcopy(first)
        first["variants"] = [{"expression": "rank(signal_a)",
                              "reason": "field A level"}]
        second["variants"] = [{"expression": "rank(signal_b)",
                               "reason": "field B level"}]
        self.model.decisions["construct_batch"] = [
            first, deepcopy(first), second, deepcopy(second)]
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        summary = agent.run_case("slow lending signal", max_batches=2,
                                 dataset_id="risk60")
        self.assertEqual(summary["status"], "BATCH_LIMIT_REACHED")
        self.assertEqual(summary["batches_completed"], 2)
        self.assertEqual(summary["new_alpha_ids"], ["A1", "A2"])
        self.assertEqual(sum(name == "simulate_batch"
                             for name, _ in self.gateway.calls), 2)

    def test_bounded_run_freezes_selected_universe(self):
        self.config["scope"]["universe"] = None
        self.gateway.single_ids = ["A1", "A2"]
        first = deepcopy(self.model.decisions["construct_batch"])
        second = deepcopy(first)
        first["variants"] = [{"expression": "rank(signal_a)", "reason": "first"}]
        second["variants"] = [{"expression": "rank(signal_b)", "reason": "second"}]
        self.model.decisions["construct_batch"] = [
            first, deepcopy(first), second, deepcopy(second)]
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        agent.run_case("slow lending signal", max_batches=2, dataset_id="risk60")
        self.assertEqual(self.model.tasks.count("select_scope"), 1)

    def test_case_stops_at_three_candidate_datasets(self):
        self.gateway.datasets = ["risk60", "other1", "other2"]
        self.gateway.single_ids = ["OLD1", "OLD2", "A3"]
        self.gateway.full_details = True
        self.gateway.correlation_by_alpha = {
            "OLD1": [["OLD2", 0.2]], "OLD2": [["OLD1", 0.2]],
            "A3": [["OLD1", 0.2], ["OLD2", 0.3]]}
        self.model.decisions["construct_batch"]["variants"] = [
            {"expression": "rank(signal_a)", "reason": "third dataset mechanism"}]
        agent = ResearchAgent(self.gateway, self.model, self.config,
                              Path(self.tmp.name))
        first = agent.run_once("slow lending signal", dataset_id="other1")
        self.assertEqual(first["gate"]["OLD1"]["status"], "CANDIDATE")
        second = agent.run_once("slow lending signal", dataset_id="other2")
        self.assertEqual(second["gate"]["OLD2"]["status"], "CANDIDATE")
        summary = agent.run_case("slow lending signal", max_batches=5,
                                 dataset_id="risk60")
        self.assertEqual(summary["status"], "TARGET_REACHED")
        self.assertEqual(summary["batches_completed"], 1)
        self.assertEqual(summary["candidate_datasets"], ["other1", "other2", "risk60"])


if __name__ == "__main__":
    unittest.main()
