"""Validator regression tests (acceptance A1): Batch 21 pre-registered
expressions must PASS; every violation class must REJECT with its code."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nastu_quant.validator import load_catalog, load_operators, validate

FIX = Path(__file__).parent / "fixtures"
CATALOG = load_catalog(str(FIX / "catalog_risk60.json"))
OPERATORS = load_operators(str(FIX / "operators.json"))

BATCH21 = [
    "group_rank(-vec_avg(rsk60_offer), market)",
    "group_zscore(-vec_avg(rsk60_offer), market)",
    "group_rank(-ts_delta(vec_avg(rsk60_offer), 66), market)",
    "group_zscore(-ts_delta(vec_avg(rsk60_offer), 66), market)",
    "group_rank(-ts_mean(vec_avg(rsk60_crowding), 20), market)",
    "group_zscore(-ts_mean(vec_avg(rsk60_crowding), 20), market)",
    "group_rank(-ts_delta(ts_mean(vec_avg(rsk60_crowding), 10), 20), market)",
    "group_rank(-vec_avg(rsk60_offer), subindustry)",
]


def check(expr, **kw):
    return validate(expr, "risk60", CATALOG, OPERATORS, **kw)


def codes(res):
    return {e["code"] for e in res["errors"]}


class TestBatch21Pass(unittest.TestCase):
    def test_all_pass(self):
        for expr in BATCH21:
            res = check(expr)
            self.assertEqual(res["verdict"], "PASS", f"{expr}: {res['errors']}")


class TestRejections(unittest.TestCase):
    def test_splice_plus(self):
        res = check("group_rank(-vec_avg(rsk60_offer) + vec_avg(rsk60_crowding), market)")
        self.assertEqual(res["verdict"], "REJECT")
        self.assertIn("E_SPLICE_OP", codes(res))

    def test_banned_multiply(self):
        res = check("group_rank(multiply(vec_avg(rsk60_offer), 2), market)")
        self.assertEqual(res["verdict"], "REJECT")
        self.assertIn("E_BANNED_OPERATOR", codes(res))

    def test_banned_trade_when(self):
        res = check("trade_when(vec_avg(rsk60_offer), vec_avg(rsk60_offer), -1)")
        self.assertEqual(res["verdict"], "REJECT")
        self.assertIn("E_BANNED_OPERATOR", codes(res))

    def test_unknown_field(self):
        res = check("group_rank(-vec_avg(rsk60_nope), market)")
        self.assertEqual(res["verdict"], "REJECT")
        self.assertIn("E_UNKNOWN_FIELD", codes(res))

    def test_mixed_dataset(self):
        res = check("group_rank(-ts_sum(pv47_spret, 5), market)")
        # pv47 field inside a risk60 batch must be rejected as mixed/unknown
        self.assertEqual(res["verdict"], "REJECT")
        self.assertTrue(codes(res) & {"E_MIXED_DATASET", "E_UNKNOWN_FIELD"})

    def test_ts_ending(self):
        res = check("ts_mean(vec_avg(rsk60_offer), 20)")
        self.assertEqual(res["verdict"], "REJECT")
        self.assertIn("E_TS_ENDING", codes(res))

    def test_vec_ending(self):
        res = check("vec_avg(rsk60_offer)")
        self.assertEqual(res["verdict"], "REJECT")
        self.assertIn("E_VEC_ENDING", codes(res))

    def test_vector_not_adapted(self):
        res = check("group_rank(-rsk60_offer, market)")
        self.assertEqual(res["verdict"], "REJECT")
        self.assertIn("E_VECTOR_NOT_ADAPTED", codes(res))

    def test_field_diff_default_reject(self):
        expr = "group_rank(vec_avg(rsk60_offer) - vec_avg(lending_fee_bid_rate), market)"
        res = check(expr)
        self.assertEqual(res["verdict"], "REJECT")
        self.assertIn("E_FIELD_DIFF_RATIO", codes(res))

    def test_field_diff_with_prereg_reason(self):
        expr = "group_rank(vec_avg(rsk60_offer) - vec_avg(lending_fee_bid_rate), market)"
        res = check(expr, allow_field_diff_ratio=True,
                    diff_ratio_reason="offer-bid spread preregistered")
        self.assertNotEqual(res["verdict"], "REJECT")
        self.assertTrue(res["warnings"], "pass-with-reason must leave a warning")

    def test_numeric_arithmetic_is_rejected(self):
        res = check("group_rank(vec_avg(rsk60_offer) / 2, market)")
        self.assertEqual(res["verdict"], "REJECT")
        self.assertIn("E_CONSTANT_ARITHMETIC", codes(res))

    def test_processed_branch_subtraction_cannot_use_raw_field_exception(self):
        expr = "group_rank(ts_mean(vec_avg(rsk60_offer), 20) - ts_mean(vec_avg(lending_fee_bid_rate), 20), market)"
        res = check(expr, allow_field_diff_ratio=True,
                    diff_ratio_reason="spread")
        self.assertEqual(res["verdict"], "REJECT")
        self.assertIn("E_PROCESSED_SUBTRACTION", codes(res))

    def test_infix_and_unary_operators_count_toward_budget(self):
        res = check("group_rank(-vec_avg(rsk60_offer), market)")
        self.assertEqual(res["operator_count"], 3)


if __name__ == "__main__":
    unittest.main()
