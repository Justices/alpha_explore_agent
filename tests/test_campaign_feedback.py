"""Regression tests for bounded, scope-matched historical campaign feedback."""
import json
import tempfile
import unittest
from pathlib import Path

from alpha_research.workflows.regular.campaign_report import prior_campaign_findings


class CampaignFeedbackTests(unittest.TestCase):
    def test_feedback_is_scope_matched_bounded_and_summary_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            combinations = []
            for index in range(5):
                combinations.append({
                    "dataset_id": "analyst10", "region": "USA",
                    "delay": 1, "universe": "TOP3000",
                    "status": "completed", "alpha_count": 1,
                    "gate_counts": {"BLOCKED": 1},
                    "common_failures": {"LOW_SHARPE": 1},
                    "best_sharpe": {"alpha_id": f"A{index}", "sharpe": 0.2},
                    "attribution_status": "complete",
                    "attribution": ["historical result " + ("x" * 500)],
                })
            combinations.append({
                **combinations[0], "dataset_id": "other_dataset",
                "best_sharpe": {"alpha_id": "OUTSIDE", "sharpe": 9.9},
            })
            (root / "prior_summary.json").write_text(json.dumps({
                "campaign_id": "prior", "objective": "improve quality",
                "updated_at": "2026-11-20T00:00:00Z",
                "combinations": combinations,
            }), encoding="utf-8")
            items = [{
                "dataset_id": "analyst10", "region": "USA",
                "delay": 1, "universe": "TOP3000",
            }]
            feedback = prior_campaign_findings(root, "current", items)
            rows = feedback[("analyst10", "USA", 1, "TOP3000")]
            self.assertEqual(len(rows), 3)
            self.assertTrue(all(row["campaign_id"] == "prior" for row in rows))
            self.assertTrue(all(len(text) <= 350 for row in rows
                                for text in row["attribution"]))
            self.assertNotIn("expression", rows[0])
            self.assertNotIn("OUTSIDE", json.dumps(rows))
            self.assertEqual(rows[0]["common_failures"], {"LOW_SHARPE": 1})


if __name__ == "__main__":
    unittest.main()
