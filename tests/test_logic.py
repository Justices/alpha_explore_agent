"""Pure-logic tests: gatekeeper order/thresholds, naming, state store,
metering, recovery matching."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nastu_quant import gatekeeper, metering, paths, recovery, state_store
from nastu_quant.config import DEFAULT_CONFIG


class TestConfig(unittest.TestCase):
    def test_no_implicit_research_scope_or_effective_settings(self):
        scope = DEFAULT_CONFIG["scope"]
        for key in ("region", "universe", "delay", "decay",
                    "neutralization", "truncation", "pasteurization",
                    "unit_handling", "nan_handling", "test_period", "max_trade"):
            self.assertIsNone(scope[key], key)


class TestGatekeeper(unittest.TestCase):
    def test_prod_and_self_threshold_is_inclusive(self):
        ok, _ = gatekeeper.correlation_verdict(0.6999, 0.5, {}, DEFAULT_CONFIG)
        self.assertTrue(ok)
        ok, reasons = gatekeeper.correlation_verdict(0.70, 0.5, {}, DEFAULT_CONFIG)
        self.assertTrue(ok, "current brain-flow rule allows exactly 0.70")
        ok, reasons = gatekeeper.correlation_verdict(0.7001, 0.5, {}, DEFAULT_CONFIG)
        self.assertFalse(ok)
        self.assertTrue(reasons)

    def test_pairwise_threshold(self):
        ok, _ = gatekeeper.correlation_verdict(0.1, 0.1, {"A1": 0.399}, DEFAULT_CONFIG)
        self.assertTrue(ok)
        ok, _ = gatekeeper.correlation_verdict(0.1, 0.1, {"A1": 0.40}, DEFAULT_CONFIG)
        self.assertFalse(ok)

    def test_correlation_requires_ra_passed(self):
        with self.assertRaises(gatekeeper.GateOrderError):
            gatekeeper.assert_correlation_allowed({"state": "RA_FAILED"})
        with self.assertRaises(gatekeeper.GateOrderError):
            gatekeeper.assert_correlation_allowed({"state": "CLASSIFIED"})
        gatekeeper.assert_correlation_allowed({"state": "RA_PASSED",
                                               "ra_fail_count": 0,
                                               "ra_readable": True})  # no raise

    def test_no_path_from_ra_failed(self):
        self.assertFalse(gatekeeper.can_transition("RA_FAILED", "CORR_PASSED"))
        self.assertTrue(gatekeeper.can_transition("CLASSIFIED", "RA_PASSED"))
        self.assertTrue(gatekeeper.can_transition("CORR_PASSED", "CANDIDATE"))

    def test_ra_gate(self):
        self.assertTrue(gatekeeper.ra_gate(0))
        self.assertFalse(gatekeeper.ra_gate(2))

    def test_failed_ra_blocks_correlation(self):
        detail = {"is": {"sharpe": 1.8, "fitness": 1.2, "sharpe_2y": 1.7,
                         "margin": 0.001, "turnover": 0.2}}
        checks = {"checks": [{"name": "LOW_2Y_SHARPE", "result": "FAIL", "value": 1.7}]}
        audit = gatekeeper.evaluate_candidate(detail, checks, "GLB", DEFAULT_CONFIG)
        self.assertEqual(audit["status"], "RA_FAILED")
        self.assertFalse(audit["correlation_allowed"])

    def test_pending_ra_allows_correlation_but_not_candidate(self):
        detail = {"is": {"sharpe": 1.8, "fitness": 1.2, "sharpe_2y": 1.7,
                         "margin": 0.001, "turnover": 0.2}}
        checks = {"checks": [{"name": "LOW_2Y_SHARPE", "result": "PENDING", "value": 1.7}]}
        audit = gatekeeper.evaluate_candidate(
            detail, checks, "GLB", DEFAULT_CONFIG,
            correlations={"prod": 0.7, "self": 0.7}, expected_pairwise_ids=[])
        self.assertTrue(audit["correlation_allowed"])
        self.assertEqual(audit["status"], "CHECKS_PENDING")

    def test_zero_ra_failures_allow_correlation_even_when_sharpe_fails(self):
        detail = {"is": {"sharpe": 1.2}}
        checks = {"checks": [{"name": "LOW_SHARPE", "result": "PASS"}]}
        audit = gatekeeper.evaluate_candidate(detail, checks, "GLB", DEFAULT_CONFIG)
        self.assertTrue(audit["correlation_allowed"])
        self.assertIn("sharpe", audit["failures"])

    def test_non_glb_requires_verified_operator_count_below_eight(self):
        detail = {"is": {"sharpe": 1.8, "fitness": 1.2,
                         "sharpe_2y": 1.7, "margin": 0.001,
                         "turnover": 0.2}}
        checks = {"checks": [{"name": "LOW_SHARPE", "result": "PASS"}]}
        evidence = {"correlations": {"prod": 0.2, "self": 0.3}}
        unknown = gatekeeper.evaluate_candidate(detail, checks, "USA", DEFAULT_CONFIG,
                                                 **evidence)
        self.assertIn("operator_count_unavailable", unknown["pending"])
        at_limit = gatekeeper.evaluate_candidate(detail, checks, "USA", DEFAULT_CONFIG,
                                                  operator_count=8, **evidence)
        self.assertIn("operator_count", at_limit["failures"])
        below = gatekeeper.evaluate_candidate(detail, checks, "USA", DEFAULT_CONFIG,
                                               operator_count=7, **evidence)
        self.assertEqual(below["status"], "CANDIDATE")

    def test_malformed_check_row_cannot_establish_readable_ra(self):
        detail = {"is": {"sharpe": 1.8}}
        checks = {"checks": [{"name": "LOW_SHARPE", "result": "PASS"},
                              {"result": "PASS"}]}
        audit = gatekeeper.evaluate_candidate(detail, checks, "GLB", DEFAULT_CONFIG)
        self.assertFalse(audit["correlation_allowed"])
        self.assertIn("ra_checks_unavailable", audit["pending"])

    def test_complete_gate_accepts_exact_individual_limit_and_rejects_pairwise_limit(self):
        detail = {"is": {"sharpe": 1.8, "fitness": 1.2, "sharpe_2y": 1.7,
                         "margin": 0.001, "turnover": 0.2}}
        checks = {"checks": [{"name": "LOW_2Y_SHARPE", "result": "PASS", "value": 1.7}]}
        kwargs = {"correlations": {"prod": 0.7, "self": 0.7},
                  "expected_pairwise_ids": ["OLD"]}
        audit = gatekeeper.evaluate_candidate(detail, checks, "GLB", DEFAULT_CONFIG,
                                               pairwise={"OLD": 0.39}, **kwargs)
        self.assertEqual(audit["status"], "CANDIDATE")
        audit = gatekeeper.evaluate_candidate(detail, checks, "GLB", DEFAULT_CONFIG,
                                               pairwise={"OLD": 0.4}, **kwargs)
        self.assertEqual(audit["status"], "PAIRWISE_CONFLICT")

    def test_correlation_parser_rejects_empty_self_recordset(self):
        raw = {"checks": {
            "production": {"max_correlation": 0.5,
                           "correlation_data": {"max": 0.5}},
            "self": {"max_correlation": 0.0,
                     "correlation_data": {"records": [], "count": 0}},
        }}
        parsed = gatekeeper.parse_correlations(raw, expected_pairwise_ids=[])
        self.assertEqual(parsed["prod"], 0.5)
        self.assertIsNone(parsed["self"])

    def test_correlation_parser_reads_named_pair_from_complete_recordset(self):
        raw = {"checks": {
            "production": {"max_correlation": 0.5,
                           "correlation_data": {"max": 0.5}},
            "self": {"max_correlation": 0.39, "correlation_data": {
                "count": 1, "next": None,
                "schema": {"properties": [{"name": "alphaId"},
                                          {"name": "correlation"}]},
                "records": [["OLD", 0.39]],
            }},
        }}
        parsed = gatekeeper.parse_correlations(raw, expected_pairwise_ids=["OLD"])
        self.assertEqual(parsed["self"], 0.39)
        self.assertEqual(parsed["pairwise"], {"OLD": 0.39})


class TestPaths(unittest.TestCase):
    def test_dataset_dir(self):
        d = paths.dataset_dir("GLB", 1, "TOP3000", "risk60")
        self.assertEqual(d.parts[-5:], ("runs", "GLB", "D1", "TOP3000", "risk60"))

    def test_batch_filename(self):
        name = paths.batch_filename(
            "20261023_142205", ["rsk60_offer"], "prereg", "md")
        self.assertEqual(name, "20261023_142205_rsk60_offer_prereg.md")
        with self.assertRaises(ValueError):
            paths.batch_filename("20261023_142205", ["f"], "bogus", "md")


class TestStateStore(unittest.TestCase):
    def test_prereg_hash_lock(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "prereg.md"
            p.write_text("hypotheses v1", encoding="utf-8")
            state = {}
            state_store.lock_prereg(p, state)
            self.assertTrue(state_store.verify_prereg(p, state))
            p.write_text("hypotheses v1 EDITED", encoding="utf-8")
            self.assertFalse(state_store.verify_prereg(p, state),
                             "post-lock edits must be detectable")


class TestMetering(unittest.TestCase):
    def test_token_none_means_unmeasured(self):
        s = metering.summarize([
            {"simulations": 8, "duration_s": 60, "token": None},
            {"simulations": 4, "duration_s": 30, "token": None},
        ])
        self.assertEqual(s["simulations"], 12)
        self.assertIsNone(s["token"])
        s2 = metering.summarize([{"simulations": 1, "duration_s": 1, "token": 500}])
        self.assertEqual(s2["token"], 500)


class TestRecovery(unittest.TestCase):
    def test_match_and_missing(self):
        settings = {"region": "GLB", "delay": 1}
        exprs = ["group_rank(-vec_avg(rsk60_offer), market)",
                 "group_rank(-vec_avg(rsk60_crowding), market)"]
        platform = [{"id": "A1", "regular": {"code": exprs[0]},
                     "settings": settings}]
        matched, missing = recovery.match_alphas(exprs, settings, platform)
        self.assertIn(exprs[0], matched)
        self.assertEqual(missing, [exprs[1]])

    def test_recovery_rejects_same_expression_with_different_settings(self):
        settings = {"region": "GLB", "delay": 1}
        expr = "rank(close)"
        platform = [{"id": "WRONG", "regular": {"code": expr},
                     "settings": {"region": "USA", "delay": 1}}]
        matched, missing = recovery.match_alphas([expr], settings, platform)
        self.assertEqual(matched, {})
        self.assertEqual(missing, [expr])

    def test_recovery_matches_platform_camel_case_settings(self):
        settings = {"region": "GLB", "delay": 1, "instrument_type": "EQUITY",
                    "unit_handling": "VERIFY"}
        expr = "rank(close)"
        platform = [{"id": "A1", "regular": {"code": expr},
                     "settings": {"region": "GLB", "delay": 1,
                                  "instrumentType": "EQUITY",
                                  "unitHandling": "VERIFY"}}]
        matched, missing = recovery.match_alphas([expr], settings, platform)
        self.assertEqual(missing, [])
        self.assertEqual(matched[expr]["id"], "A1")

    def test_recovery_notes_missing_platform_setting_keys(self):
        settings = {"region": "GLB", "delay": 1,
                    "instrument_type": "EQUITY"}
        expr = "rank(close)"
        platform = [{"id": "A1", "regular": {"code": expr},
                     "settings": {"region": "GLB", "delay": 1}}]
        matched, missing, notes = recovery.match_alphas_detailed(
            [expr], settings, platform)
        self.assertEqual(missing, [])
        self.assertEqual(matched[expr]["id"], "A1")
        self.assertEqual(notes[expr], ["instrumentType"])

    def test_recovery_vetoes_present_conflicting_setting_in_either_key_shape(self):
        settings = {"region": "GLB", "instrument_type": "EQUITY"}
        expr = "rank(close)"
        for observed in ({"region": "GLB", "instrumentType": "FUTURES"},
                         {"region": "GLB", "instrument_type": "FUTURES"}):
            matched, missing, _ = recovery.match_alphas_detailed(
                [expr], settings,
                [{"id": "WRONG", "regular": {"code": expr},
                  "settings": observed}])
            self.assertEqual(matched, {})
            self.assertEqual(missing, [expr])


if __name__ == "__main__":
    unittest.main()
