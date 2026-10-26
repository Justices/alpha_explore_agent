"""Pure-logic tests: gatekeeper order/thresholds, naming, state store,
metering, recovery matching."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nastu_quant import gatekeeper, metering, paths, recovery, state_store
from nastu_quant.config import DEFAULT_CONFIG


class TestGatekeeper(unittest.TestCase):
    def test_corr_threshold_is_strict(self):
        ok, _ = gatekeeper.correlation_verdict(0.6999, 0.5, {}, DEFAULT_CONFIG)
        self.assertTrue(ok)
        ok, reasons = gatekeeper.correlation_verdict(0.70, 0.5, {}, DEFAULT_CONFIG)
        self.assertFalse(ok, "exactly 0.70 must FAIL after the tightening")
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
        gatekeeper.assert_correlation_allowed({"state": "RA_PASSED"})  # no raise

    def test_no_path_from_ra_failed(self):
        self.assertFalse(gatekeeper.can_transition("RA_FAILED", "CORR_PASSED"))
        self.assertTrue(gatekeeper.can_transition("CLASSIFIED", "RA_PASSED"))
        self.assertTrue(gatekeeper.can_transition("CORR_PASSED", "CANDIDATE"))

    def test_ra_gate(self):
        self.assertTrue(gatekeeper.ra_gate(0))
        self.assertFalse(gatekeeper.ra_gate(2))


class TestPaths(unittest.TestCase):
    def test_dataset_dir(self):
        d = paths.dataset_dir("GLB", 1, "TOP3000", "risk60")
        self.assertTrue(str(d).endswith("runs/GLB/D1/TOP3000/risk60"))

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
        platform = [{"id": "A1", "regular": {"code": exprs[0]}}]
        matched, missing = recovery.match_alphas(exprs, settings, platform)
        self.assertIn(exprs[0], matched)
        self.assertEqual(missing, [exprs[1]])


if __name__ == "__main__":
    unittest.main()
