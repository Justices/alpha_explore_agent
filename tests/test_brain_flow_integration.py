import json
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nastu_quant.agent import ResearchAgent, ResearchError, _validate_test_period
from nastu_quant.platform_runtime import PlatformRuntime
from nastu_quant.config import DEFAULT_CONFIG
from nastu_quant.gatekeeper import evaluate_candidate


class TestBrainFlowRecovery(unittest.TestCase):
    def test_legacy_probe_draft_without_runner_ids_stops_before_model_call(self):
        agent = ResearchAgent.__new__(ResearchAgent)
        agent._decide = lambda *args, **kwargs: self.fail("model must not be called")
        with tempfile.TemporaryDirectory() as tmp:
            batch = Path(tmp)
            (batch / "batch_context.json").write_text(json.dumps({
                "objective": "fundamental forecast", "scope": {"region": "GLB"},
                "dataset_id": "analyst69", "fields": []
            }), encoding="utf-8")
            with self.assertRaisesRegex(ResearchError, "runner reference"):
                agent._recover_probes(batch)

    def test_resumed_runner_results_are_saved_in_agent_result_format(self):
        runner = {"case_key": "case", "run_id": "run", "batch_id": "batch",
                  "label": "production"}
        metadata = {"case_key": "case", "run_id": "run", "output": "result.json"}
        row = {"name": "variant_1", "expression": "rank(field)", "success": True,
               "alpha_id": "A1", "is": {"sharpe": 1.8}}

        class Gateway:
            def call(self, command, **params):
                if command != "resume_simulation_batch" or params != runner:
                    raise AssertionError("unexpected recovery request")
                return {"results": [row], "runner": metadata}

        agent = ResearchAgent.__new__(ResearchAgent)
        agent.gateway = Gateway()
        with tempfile.TemporaryDirectory() as tmp:
            batch_dir = Path(tmp)
            raw_path = batch_dir / "simulation_raw.json"
            recovered = agent._resume_canonical_batch(
                {"runner": runner}, batch_dir, raw_path, "production")

            self.assertIsNotNone(recovered)
            self.assertEqual(recovered["alpha_results"][0]["alpha_id"], "A1")
            self.assertEqual(recovered["alpha_results"][0]["details"]["is"], row["is"])
            self.assertEqual(recovered["runner"], metadata)
            self.assertEqual(json.loads(raw_path.read_text(encoding="utf-8")), recovered)
            status = json.loads((batch_dir / "production_runner_recovery.json").read_text(
                encoding="utf-8"))
            self.assertEqual(status["status"], "RESUMED")


class TestIndependentSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        class Client:
            def request(self, method, path, *, params=None, json=None):
                self.payload = json
                return type("Response", (), {"status_code": 201,
                            "headers": {"Location": "/simulations/S1"}})()
            def close(self):
                pass
        self.client = Client()
        self.runtime = PlatformRuntime(Path(self.tmp.name), client=self.client)
        self.addCleanup(self.runtime.close)
        self.runtime.set_case("case", "run")
        self.runtime._admission = lambda request: []
        self.runtime._finish = lambda request, paths, progress: self.client.payload
        self.settings = {
            "instrument_type": "EQUITY", "region": "GLB", "universe": "TOP3000",
            "delay": 1, "decay": 4, "neutralization": "MARKET",
            "truncation": 0.08, "pasteurization": "ON", "unit_handling": "VERIFY",
            "nan_handling": "OFF", "max_trade": "OFF", "language": "FASTEXPR",
            "visualization": False, "test_period": "P0Y0M",
        }

    def _run(self):
        return self.runtime.run_simulation_batch({
            "batch_id": "20261028_120000_abcdef12", "label": "production",
            "dataset_id": "analyst69", "settings": self.settings,
            "variants": [{"name": "variant_1", "expression": "rank(field_a)"},
                         {"name": "variant_2", "expression": "rank(field_b)"}],
        })

    def test_test_period_is_forwarded_in_every_runner_variant(self):
        self.settings["test_period"] = "P0Y0M"
        rows = self._run()
        self.assertEqual([row["settings"]["testPeriod"] for row in rows],
                         ["P0Y0M", "P0Y0M"])

    def test_pasteurization_override_is_forwarded_to_runner(self):
        self.settings["pasteurization"] = "OFF"
        rows = self._run()
        self.assertEqual([row["settings"]["pasteurization"] for row in rows],
                         ["OFF", "OFF"])


class TestTestPeriodSchema(unittest.TestCase):
    def setUp(self):
        self.rule = {"type": "string", "minValue": "P0Y0M0D", "maxValue": "P6Y0M0D"}

    def test_inclusive_bounds_and_supported_short_form(self):
        for value in ("P0Y0M", "P0Y0M30D", "P6Y0M0D"):
            with self.subTest(value=value):
                self.assertEqual(_validate_test_period(value, self.rule)["value"], value)

    def test_out_of_range_or_ambiguous_durations_are_blocked(self):
        for value in ("P6Y0M1D", "P5Y11M31D", "not-a-duration"):
            with self.subTest(value=value), self.assertRaises(ResearchError):
                _validate_test_period(value, self.rule)

    def test_missing_or_choice_schema_is_handled_conservatively(self):
        with self.assertRaises(ResearchError):
            _validate_test_period("P0Y0M", None)
        choices = {"choices": [{"value": "P0Y0M"}, {"value": "P1Y0M"}]}
        self.assertEqual(_validate_test_period("P1Y0M", choices)["value"], "P1Y0M")
        with self.assertRaises(ResearchError):
            _validate_test_period("P2Y0M", choices)
        for rule in ({"choices": [{"value": {}}]},
                     {"type": "integer", "minValue": "P0Y0M0D", "maxValue": "P6Y0M0D"}):
            with self.subTest(rule=rule), self.assertRaises(ResearchError):
                _validate_test_period("P0Y0M", rule)


class TestCanonicalAuditPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.batch = Path(self.tmp.name)
        output = self.batch / "runner.json"
        output.with_suffix(".admission.json").write_text(json.dumps({
            "variants": [{"name": "variant_1", "expression": "rank(field)"}]
        }), encoding="utf-8")
        (self.batch / "simulation_raw.json").write_text(json.dumps({
            "runner": {"case_key": "case", "run_id": "run", "output": str(output),
                       "field_cache_root": str(self.batch / "fields")}
        }), encoding="utf-8")
        self.settings = {"instrument_type": "EQUITY", "region": "GLB",
                         "universe": "TOP3000", "delay": 1, "decay": 4,
                         "neutralization": "MARKET", "truncation": 0.08,
                         "pasteurization": "ON", "unit_handling": "VERIFY",
                         "nan_handling": "OFF", "language": "FASTEXPR",
                         "visualization": False, "test_period": "P0Y0M",
                         "max_trade": "OFF"}
        self.details = {"id": "A1", "type": "REGULAR",
                        "settings": {"instrumentType": "EQUITY", "region": "GLB",
                                     "universe": "TOP3000", "delay": 1, "decay": 4,
                                     "neutralization": "MARKET", "truncation": 0.08,
                                     "pasteurization": "ON", "unitHandling": "VERIFY",
                                     "nanHandling": "OFF", "language": "FASTEXPR",
                                     "visualization": False, "testPeriod": "P0Y0M",
                                     "maxTrade": "OFF"},
                        "regular": {"code": "rank(field)"},
                        "is": {"sharpe": 1.5, "fitness": 1.2, "sharpe_2y": 1.8,
                               "margin": 0.001, "turnover": 0.2,
                               "checks": [{"name": "LOW_SHARPE", "result": "PASS"}]}}
        self.config = deepcopy(DEFAULT_CONFIG)
        self.config["gate"].update({"sharpe_min": 1.4, "prod_corr_max": 0.8,
                                    "self_corr_max": 0.8, "pairwise_corr_max": 0.5})
        self.correlations = {"prod": 0.4, "self": 0.5, "pairwise": {}}
        self.prereg = {"dataset_id": "analyst69", "scope": {"region": "GLB",
                        "universe": "TOP3000", "delay": 1},
                       "simulation_settings": deepcopy(self.settings),
                       "variants": [{"expression": "rank(field)"}],
                       "operator_counts": {"rank(field)": 1}}
        self.readback = {"A1": {"details": deepcopy(self.details),
                                "review": {"verdict": "CLEAR", "reason": "reviewed",
                                           "evidence_refs": ["readback"]}}}
        self.audit = {"alpha_id": "A1", "candidate_eligible": True,
                      "candidate_status": "candidate", "candidate_metric_failures": [],
                      "gate_failures": [], "gate_pending": [],
                      "raw_details": deepcopy(self.details), "prod_corr": 0.4,
                      "self_corr": 0.5, "expression": "rank(field)",
                      "field_admission": {"passed": True, "dataset": "analyst69",
                                          "scope": {"region": "GLB", "universe": "TOP3000",
                                                    "delay": 1, "instrument_type": "EQUITY"},
                                          "fields": [{"id": "field"}], "failures": []},
                      "pairwise_state": "not_applicable", "pairwise": []}
        self.audit_rows = [self.audit]
        fixture = self

        class Gateway:
            def call(self, command, **params):
                if command != "audit_alphas":
                    raise AssertionError("unexpected audit request")
                return {"results": fixture.audit_rows, "audit_path": "audit.json"}

        self.agent = ResearchAgent.__new__(ResearchAgent)
        self.agent.gateway = Gateway()
        self.agent.config = self.config

    def _run(self):
        verdict = evaluate_candidate(
            self.details, {"checks": self.details["is"]["checks"]}, "GLB", self.config,
            correlations=self.correlations, pairwise=self.correlations["pairwise"],
            expected_pairwise_ids=list(self.correlations["pairwise"]), operator_count=1)
        verdict["correlations"] = deepcopy(self.correlations)
        gate = {"A1": verdict}
        self.agent._apply_canonical_audit(
            self.batch, self.prereg, ["A1"], self.readback, gate)
        return gate["A1"]

    def test_external_metric_threshold_does_not_override_project_threshold(self):
        self.audit.update({"candidate_eligible": False, "candidate_status": "failed",
                           "candidate_metric_failures": ["primary_sharpe"],
                           "gate_failures": ["primary_sharpe"]})
        verdict = self._run()
        self.assertEqual(verdict["status"], "CANDIDATE")
        self.assertEqual(verdict["canonical_audit"]["gate_failures"], ["primary_sharpe"])
        self.assertEqual(verdict["canonical_audit"]["gate_config"]["sharpe_min"], 1.4)

    def test_regular_audit_does_not_enforce_ppa_operator_or_field_limits(self):
        self.prereg["operator_counts"]["rank(field)"] = 9
        self.audit["field_admission"]["fields"] = [{"id": name} for name in
                                                   ("field", "field_b", "field_c", "field_d")]
        self.assertEqual(self._run()["status"], "CANDIDATE")

    def test_ppa_audit_rechecks_structural_evidence(self):
        self.prereg["research_type"] = "PPA"
        self.prereg["operator_counts"]["rank(field)"] = 9
        self.prereg["validation"] = {"rank(field)": {"fields": ["field"], "operator_count": 9}}
        verdict = self._run()
        self.assertNotEqual(verdict["status"], "CANDIDATE")
        self.assertIn("ppa_operator_count", verdict["failures"])

    def test_ppa_audit_requires_fields_to_match_pre_backtest_evidence(self):
        self.prereg["research_type"] = "PPA"
        self.prereg["validation"] = {"rank(field)": {"fields": ["field", "other"], "operator_count": 1}}
        verdict = self._run()
        self.assertEqual(verdict["status"], "PENDING_FULL_AUDIT")
        self.assertIn("ppa_field_count_unavailable", verdict["pending"])

    def test_ppa_audit_enforces_one_sharpe_even_with_a_lower_candidate_setting(self):
        self.config["gate"]["sharpe_min"] = 0.5
        for details in (self.details, self.readback["A1"]["details"], self.audit["raw_details"]):
            details["is"]["sharpe"] = 0.99
        self.prereg["research_type"] = "PPA"
        self.prereg["validation"] = {"rank(field)": {"fields": ["field"], "operator_count": 1}}
        verdict = self._run()
        self.assertNotEqual(verdict["status"], "CANDIDATE")
        self.assertIn("ppa_sharpe", verdict["failures"])

    def test_complete_local_evidence_does_not_override_project_metric_failure(self):
        self.audit.pop("candidate_eligible")
        self.audit.update({"audit_kind": "local_evidence", "evidence_complete": True,
                           "candidate_status": "evidence_complete"})
        self.config["gate"]["sharpe_min"] = 1.6
        verdict = self._run()
        self.assertNotEqual(verdict["status"], "CANDIDATE")
        self.assertTrue(verdict["canonical_audit"]["evidence_complete"])
        self.assertIn("sharpe", verdict["failures"])

    def test_custom_gate_configuration_is_validated_without_forcing_defaults(self):
        self.agent._assert_gate_config()
        self.config["gate"].pop("risk_neutralized_sharpe_min")
        self.config["gate"].pop("risk_neutralized_fitness_min")
        self.agent._assert_gate_config()
        for value in (float("nan"), float("inf"), True, "0.7"):
            with self.subTest(value=value):
                self.config["gate"]["prod_corr_max"] = value
                with self.assertRaises(ResearchError):
                    self.agent._assert_gate_config()
        self.config["gate"]["prod_corr_max"] = 1.01
        with self.assertRaises(ResearchError):
            self.agent._assert_gate_config()
        self.config["gate"]["prod_corr_max"] = 0.7
        self.config["gate"]["turnover_min"] = 0.4
        self.config["gate"]["turnover_max"] = 0.3
        with self.assertRaises(ResearchError):
            self.agent._assert_gate_config()

    def test_stricter_project_metric_threshold_is_never_overridden(self):
        self.config["gate"]["sharpe_min"] = 1.6
        self.assertNotEqual(self._run()["status"], "CANDIDATE")

    def test_risk_neutralized_metrics_use_project_thresholds(self):
        self.config["gate"].update({"risk_neutralized_sharpe_min": 0.8,
                                    "risk_neutralized_fitness_min": 0.6})
        risk = {"sharpe": 0.9, "fitness": 0.65, "margin": 0.001}
        self.details["is"]["riskNeutralized"] = deepcopy(risk)
        self.readback["A1"]["details"]["is"]["riskNeutralized"] = deepcopy(risk)
        self.audit["raw_details"]["is"]["riskNeutralized"] = deepcopy(risk)
        self.audit.update({"candidate_eligible": False, "candidate_status": "failed",
                           "candidate_metric_failures": ["risk_neutralized_sharpe",
                                                         "risk_neutralized_fitness"],
                           "gate_failures": ["risk_neutralized_sharpe",
                                             "risk_neutralized_fitness"]})
        self.assertEqual(self._run()["status"], "CANDIDATE")

    def test_legacy_config_uses_existing_risk_defaults(self):
        self.config["gate"].pop("risk_neutralized_sharpe_min")
        self.config["gate"].pop("risk_neutralized_fitness_min")
        self.details["is"]["riskNeutralized"] = {
            "sharpe": 0.9, "fitness": 0.65, "margin": 0.001}
        verdict = evaluate_candidate(
            self.details, {"checks": self.details["is"]["checks"]}, "GLB", self.config,
            correlations=self.correlations, operator_count=1)
        self.assertIn("risk_neutralized_sharpe", verdict["failures"])
        self.assertIn("risk_neutralized_fitness", verdict["failures"])

    def test_unreadable_risk_threshold_remains_pending(self):
        self.config["gate"]["risk_neutralized_sharpe_min"] = None
        self.details["is"]["riskNeutralized"] = {
            "sharpe": 1.2, "fitness": 0.9, "margin": 0.001}
        verdict = evaluate_candidate(
            self.details, {"checks": self.details["is"]["checks"]}, "GLB", self.config,
            correlations=self.correlations, operator_count=1)
        self.assertIn("risk_neutralized_sharpe_threshold_unavailable", verdict["pending"])

    def test_unverified_research_evidence_remains_pending(self):
        self.audit.update({"candidate_eligible": False, "candidate_status": "checks_pending",
                           "gate_pending": ["pre_backtest_evidence_unverified"]})
        verdict = self._run()
        self.assertEqual(verdict["status"], "PENDING_FULL_AUDIT")
        self.assertIn("canonical_audit:pre_backtest_evidence_unverified", verdict["pending"])

    def test_unknown_audit_failure_is_pending(self):
        self.audit.update({"candidate_eligible": False, "candidate_status": "failed",
                           "gate_failures": ["future_unknown_gate"]})
        verdict = self._run()
        self.assertEqual(verdict["status"], "PENDING_FULL_AUDIT")
        self.assertTrue(any("future_unknown_gate" in item for item in verdict["pending"]))

    def test_external_pair_conflict_uses_project_limit(self):
        self.correlations["pairwise"] = {"B1": 0.45}
        self.audit.update({"candidate_eligible": False, "candidate_status": "pairwise_conflict",
                           "gate_pending": ["pairwise_conflict_requires_user_choice"],
                           "pairwise_state": "evaluated",
                           "pairwise": [{"pair": ["A1", "B1"], "corr": 0.45,
                                         "verdict": "conflict"}]})
        self.assertEqual(self._run()["status"], "CANDIDATE")

    def test_additional_audit_pair_obeys_project_limit(self):
        self.config["gate"]["pairwise_corr_max"] = 0.4
        self.audit.update({"candidate_eligible": False, "candidate_status": "pairwise_conflict",
                           "gate_pending": ["pairwise_conflict_requires_user_choice"],
                           "pairwise_state": "evaluated",
                           "pairwise": [{"pair": ["A1", "B1"], "corr": 0.45,
                                         "verdict": "conflict"}]})
        verdict = self._run()
        self.assertEqual(verdict["status"], "PAIRWISE_CONFLICT")
        self.assertIn("pairwise:B1", verdict["failures"])

    def test_external_correlation_failure_uses_project_limit(self):
        self.correlations["self"] = 0.75
        self.audit.update({"candidate_eligible": False, "candidate_status": "failed",
                           "self_corr": 0.75,
                           "gate_failures": ["individual_correlation_exceeded"]})
        self.assertEqual(self._run()["status"], "CANDIDATE")

    def test_self_correlation_at_project_limit_is_allowed(self):
        self.correlations["self"] = 0.8
        self.audit.update({"candidate_eligible": False, "candidate_status": "failed",
                           "self_corr": 0.8,
                           "gate_failures": ["individual_correlation_exceeded"]})
        self.assertEqual(self._run()["status"], "CANDIDATE")

    def test_pairwise_correlation_at_project_limit_conflicts(self):
        self.audit.update({"candidate_eligible": False, "candidate_status": "pairwise_conflict",
                           "gate_pending": ["pairwise_conflict_requires_user_choice"],
                           "pairwise_state": "evaluated",
                           "pairwise": [{"pair": ["A1", "B1"], "corr": 0.5,
                                         "verdict": "conflict"}]})
        verdict = self._run()
        self.assertEqual(verdict["status"], "PAIRWISE_CONFLICT")
        self.assertIn("pairwise:B1", verdict["failures"])

    def test_audit_correlation_uses_stricter_project_limit(self):
        self.config["gate"]["prod_corr_max"] = 0.6
        self.audit["prod_corr"] = 0.65
        verdict = self._run()
        self.assertEqual(verdict["status"], "CORR_REJECTED")
        self.assertIn("prod_correlation", verdict["failures"])

    def test_unavailable_audit_correlation_requires_complete_local_evidence(self):
        self.correlations["self"] = 0.75
        self.audit.update({"candidate_eligible": False, "candidate_status": "checks_pending",
                           "prod_corr": None, "self_corr": None,
                           "gate_pending": ["individual_correlation_unavailable"]})
        self.assertEqual(self._run()["status"], "PENDING_FULL_AUDIT")
        self.readback["A1"]["correlation_raw"] = {"checks": {
            "production": {"correlation_data": {"max": 0.4}},
            "self": {"correlation_data": {"count": 1, "next": None,
                "schema": {"properties": [{"name": "alphaId"}, {"name": "correlation"}]},
                "records": [["OTHER", 0.75]]}}}}
        self.assertEqual(self._run()["status"], "CANDIDATE")

    def test_invalid_audit_pair_is_unresolved(self):
        self.audit.update({"candidate_eligible": False, "candidate_status": "checks_pending",
                           "gate_pending": ["pairwise_correlation_unavailable"],
                           "pairwise_state": "evaluated",
                           "pairwise": [{"pair": ["A1", "B1"], "corr": None,
                                         "verdict": "pending"}]})
        verdict = self._run()
        self.assertEqual(verdict["status"], "PENDING_FULL_AUDIT")
        self.assertIn("canonical_audit:pairwise_evidence_unavailable", verdict["pending"])

    def test_later_platform_failure_is_not_ignored(self):
        self.audit["raw_details"]["is"]["checks"][0]["result"] = "FAIL"
        self.audit.update({"candidate_eligible": False, "candidate_status": "failed",
                           "candidate_metric_failures": ["ra_failed_count"],
                           "gate_failures": ["ra_failed_count"]})
        verdict = self._run()
        self.assertNotEqual(verdict["status"], "CANDIDATE")
        self.assertIn("ra:LOW_SHARPE", verdict["failures"])

    def test_other_platform_check_failure_is_not_ignored(self):
        check = {"name": "NEW_PLATFORM_CHECK", "result": "FAIL"}
        for details in (self.details, self.readback["A1"]["details"],
                        self.audit["raw_details"]):
            details["is"]["checks"].append(deepcopy(check))
        verdict = self._run()
        self.assertNotEqual(verdict["status"], "CANDIDATE")
        self.assertIn("platform_check:NEW_PLATFORM_CHECK", verdict["failures"])

    def test_other_pending_platform_check_is_unresolved(self):
        check = {"name": "NEW_PLATFORM_CHECK", "result": "PENDING"}
        for details in (self.details, self.readback["A1"]["details"],
                        self.audit["raw_details"]):
            details["is"]["checks"].append(deepcopy(check))
        verdict = self._run()
        self.assertEqual(verdict["status"], "PENDING_FULL_AUDIT")
        self.assertIn("platform_check_pending:NEW_PLATFORM_CHECK", verdict["pending"])

    def test_duplicate_or_incomplete_audit_rows_are_pending(self):
        self.audit_rows = [self.audit, deepcopy(self.audit)]
        self.assertEqual(self._run()["status"], "PENDING_FULL_AUDIT")
        self.audit_rows = [{"alpha_id": "A1", "candidate_eligible": True}]
        self.assertEqual(self._run()["status"], "PENDING_FULL_AUDIT")

    def test_mismatched_admission_or_settings_are_pending(self):
        self.audit["field_admission"]["dataset"] = "other"
        self.assertEqual(self._run()["status"], "PENDING_FULL_AUDIT")
        self.audit["field_admission"]["dataset"] = "analyst69"
        self.audit["raw_details"]["settings"]["testPeriod"] = "P1Y0M"
        self.assertEqual(self._run()["status"], "PENDING_FULL_AUDIT")

    def test_changed_readback_remains_pending_even_if_metrics_pass(self):
        self.audit["raw_details"]["is"]["sharpe"] = 1.7
        verdict = self._run()
        self.assertEqual(verdict["status"], "PENDING_FULL_AUDIT")
        self.assertIn("canonical_audit:readback_changed", verdict["pending"])


if __name__ == "__main__":
    unittest.main()
