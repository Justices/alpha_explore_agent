"""Independent, evidence-preserving candidate-pair correlation checks."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nastu_quant.correlation import parse_pnl_recordset, pairwise_pnl_correlation
from nastu_quant.gatekeeper import local_self_correlation_state, parse_correlations


def pnl(values):
    return {"count": len(values), "next": None,
            "schema": {"properties": [{"name": "date"}, {"name": "pnl"}]},
            "records": [[f"2024-01-{index + 1:02d}", value]
                        for index, value in enumerate(values)]}


class TestLocalPairwise(unittest.TestCase):
    def test_correlation_uses_daily_changes_of_cumulative_pnl(self):
        result = pairwise_pnl_correlation(pnl([0, 1, 3, 6]), pnl([0, 2, 6, 12]))
        self.assertAlmostEqual(result["correlation"], 1.0)
        self.assertEqual(result["observations"], 3)
        self.assertEqual(result["source"], "daily_pnl")

    def test_large_finite_pnl_does_not_overflow_the_denominator(self):
        result = pairwise_pnl_correlation(pnl([0, 1e77, 3e77, 6e77]),
                                          pnl([0, 2e77, 6e77, 12e77]))
        self.assertAlmostEqual(result["correlation"], 1.0)

    def test_recent_window_excludes_intervals_starting_before_cutoff(self):
        left = pnl([0, 1, 3, 6, 10])
        right = pnl([0, 1, 3, 6, 10])
        dates = ["2020-01-01", "2025-01-01", "2025-01-02", "2025-01-03", "2025-01-04"]
        for recordset in (left, right):
            for row, day in zip(recordset["records"], dates):
                row[0] = day
        result = pairwise_pnl_correlation(left, right, window_years=4)
        self.assertAlmostEqual(result["correlation"], 1.0)
        self.assertEqual(result["observations"], 3)

    def test_missing_incomplete_or_degenerate_pnl_is_unresolved(self):
        incomplete = pnl([0, 1, 3])
        incomplete["next"] = "another page"
        duplicate = pnl([0, 1, 3])
        duplicate["records"][2][0] = duplicate["records"][1][0]
        for invalid in (None, incomplete, duplicate):
            with self.subTest(invalid=invalid):
                self.assertIsNone(parse_pnl_recordset(invalid))
                self.assertIsNone(pairwise_pnl_correlation(invalid, pnl([0, 1, 3])))
        self.assertIsNone(pairwise_pnl_correlation(pnl([0, 1, 2, 3]), pnl([0, 2, 4, 6])))
        self.assertIsNone(pairwise_pnl_correlation(pnl([0, 1]), pnl([0, 2])))

    def test_gate_parser_accepts_only_measured_local_pairwise_evidence(self):
        raw = {"checks": {"production": {"correlation_data": {"max": 0.2}},
            "self": {"correlation_data": {"count": 1, "next": None,
                "schema": {"properties": [{"name": "alphaId"}, {"name": "correlation"}]},
                "records": [["unrelated", 0.3]]}}},
            "case_pairwise": {"B1": {"source": "daily_pnl", "observations": 3,
                                      "correlation": -0.6}}}
        parsed = parse_correlations(raw, ["B1"])
        self.assertEqual(parsed["pairwise"]["B1"], 0.6)
        raw["case_pairwise"]["B1"]["observations"] = 1
        self.assertNotIn("B1", parse_correlations(raw, ["B1"])["pairwise"])

    def test_production_correlation_is_not_inferred_from_schema_metadata(self):
        raw = {"checks": {"production": {"correlation_data": {"schema": {"max": 0.1}}}}}
        self.assertIsNone(parse_correlations(raw)["prod"])

    def test_malformed_self_schema_cannot_shift_the_correlation_column(self):
        raw = {"checks": {"self": {"correlation_data": {
            "count": 1, "records": [[0.1, 0.8]],
            "schema": {"properties": [None, {"name": "correlation"}]}}}}}
        self.assertIsNone(parse_correlations(raw)["self"])

    def test_local_prefilter_requires_complete_matching_evidence(self):
        evidence = {"status": "pass", "value": 0.6, "complete": True,
                    "comparison_count": 2, "window_years": 4, "limit": 0.7}
        self.assertEqual(local_self_correlation_state(evidence, 0.7), "pass")
        self.assertEqual(local_self_correlation_state({**evidence, "complete": False}, 0.7), "pending")
        self.assertEqual(local_self_correlation_state({**evidence, "value": 0.8}, 0.7), "fail")
        self.assertEqual(local_self_correlation_state(evidence, 0.6), "pending")


if __name__ == "__main__":
    unittest.main()
