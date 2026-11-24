"""All-offline round-two shortlist verification and coverage-risk guardrails."""
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from alpha_research.cli_canary import FIELD, run_canary_command
from alpha_research.cli_canary_design import design_canary
from alpha_research.cli_canary_shortlist import shortlist_canary
from alpha_research.state.store import atomic_write_json
from tests.test_canary_round2_design import CanaryRoundTwoDesignTests


class RoundTwoShortlistTests(unittest.TestCase):
    def setUp(self):
        previous = CanaryRoundTwoDesignTests(
            "test_three_candidates_have_real_signature_pass_and_are_not_authorized")
        previous.setUp()
        self.addCleanup(previous.doCleanups)
        self.previous = previous
        self.root = previous.root
        self.plan_id = previous.plan_id
        self.snapshots = previous.snapshots
        previous.fields[0]["coverage"] = 0.2657
        previous.fields.extend([
            {"id": "analyst_revision_count_ebitda_q1", "type": "MATRIX",
             "dataset": {"id": "analyst10"},
             "description": "Count of analyst EBITDA forecast revisions, first quarter",
             "coverage": 0.78},
            {"id": "analyst_innovative_increase_ebitda_q1", "type": "MATRIX",
             "dataset": {"id": "analyst10"},
             "description": "Count of innovative upward revisions in analyst EBITDA estimates",
             "coverage": 0.61},
            {"id": "analyst_revision_eps_q1", "type": "MATRIX",
             "dataset": {"id": "analyst10"},
             "description": "EPS analyst revision count", "coverage": 0.95},
            {"id": "analyst_revision_ebitda_q4", "type": "MATRIX",
             "dataset": {"id": "other-dataset"},
             "description": "Analyst revisions for EBITDA", "coverage": 0.90},
            {"id": "analyst_revision_ebitda_q3", "type": "MATRIX",
             "dataset": {"id": "analyst10"},
             "description": "Analyst EBITDA revisions with unknown coverage"},
        ])
        previous._save_catalogs()
        canary = json.loads(previous.legacy.canary_path.read_text(encoding="utf-8"))
        canary["catalogs"]["field_count"] = len(previous.fields)
        atomic_write_json(previous.legacy.canary_path, canary)
        design_canary(self.root, self.plan_id, snapshot_root=self.snapshots)

    def test_prioritization_does_not_claim_real_performance_or_simulation_permission(self):
        result = shortlist_canary(self.root, self.plan_id, snapshot_root=self.snapshots)
        self.assertEqual(result["status"], "PRIORITIZED_OFFLINE_COVERAGE_REVIEW")
        self.assertEqual(result["coverage_screen"]["catalog_coverage"], 0.2657)
        self.assertEqual(result["coverage_screen"]["uncovered_fraction"], 0.7343)
        self.assertEqual(result["coverage_screen"]["risk"], "HIGH_COVERAGE_RISK")
        self.assertFalse(result["coverage_screen"]["threshold_is_platform_gate"])
        self.assertTrue(result["coverage_screen"]["coverage_is_not_position_coverage"])
        self.assertEqual(
            [p["proposal_id"] for p in result["research_order_not_performance_rank"]],
            ["R2_SMOOTH_10", "R2_RELATIVE_20", "R2_PERSISTENCE_5"])
        for p in result["research_order_not_performance_rank"]:
            self.assertFalse(p["ready_for_simulation"])
            self.assertFalse(p["effective_alpha_coverage_verified"])
            self.assertFalse(p["concentration_improvement_verified"])
        self.assertFalse(result["simulation_authorized"])
        self.assertFalse(result["simulation_invoked"])
        self.assertFalse(result["model_invoked"])
        self.assertFalse(result["candidate_eligible"])
        self.assertFalse(result["alpha_submission_invoked"])
        self.assertEqual(shortlist_canary(
            self.root, self.plan_id, snapshot_root=self.snapshots)["status"],
            "PRIORITIZED_OFFLINE_COVERAGE_REVIEW")

    def test_related_fields_are_only_grounded_catalog_rows_without_auto_substitution(self):
        result = shortlist_canary(self.root, self.plan_id, snapshot_root=self.snapshots)
        group = result["related_datafields"]
        self.assertEqual(group["matched_total_with_readable_coverage"], 2)
        self.assertEqual([row["id"] for row in group["candidates"]], [
            "analyst_revision_count_ebitda_q1",
            "analyst_innovative_increase_ebitda_q1"])
        self.assertEqual(group["candidates"][0]["coverage"], 0.78)
        self.assertFalse(group["candidates"][0]["semantic_equivalence_verified"])
        self.assertFalse(group["candidates"][0]["substitution_approved"])
        self.assertFalse(group["candidates"][0]["static_expression_verified"])
        self.assertTrue(group["substitutions_not_authorized"])

    def test_tampered_stored_design_rejected_and_shortlist_not_created(self):
        path = (self.root / "state" / "canary" /
                (self.plan_id + "_round2_design.json"))
        report = json.loads(path.read_text(encoding="utf-8"))
        report["counts"]["static_pass"] = 123
        atomic_write_json(path, report)
        with self.assertRaisesRegex(Exception, "existing round-two design changed"):
            shortlist_canary(self.root, self.plan_id, snapshot_root=self.snapshots)
        shortlist_path = self.root / "state" / "canary" / (
            self.plan_id + "_round2_shortlist.json")
        self.assertFalse(shortlist_path.exists())

    def test_no_gateway_credentials_model_or_simulation_required(self):
        args = SimpleNamespace(command="canary-shortlist", plan_id=self.plan_id)
        out = StringIO()
        with (patch("alpha_research.cli_canary_shortlist.CATALOG_SNAPSHOT_ROOT",
                    self.snapshots),
              patch.dict("os.environ", {"BRAIN_CREDENTIALS_FILE": ""}),
              redirect_stdout(out)):
            code = run_canary_command(
                args, self.root,
                gateway_factory=lambda: self.fail("Gateway must not be constructed"))
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["status"], "PRIORITIZED_OFFLINE_COVERAGE_REVIEW")
        self.assertFalse(result["simulation_invoked"])
        self.assertFalse(result["alpha_submission_invoked"])

    def test_prior_shortlist_immutable(self):
        result = shortlist_canary(self.root, self.plan_id, snapshot_root=self.snapshots)
        path = Path(result["shortlist_path"])
        data = json.loads(path.read_text(encoding="utf-8"))
        data["simulation_authorized"] = True
        atomic_write_json(path, data)
        with self.assertRaisesRegex(Exception, "prior shortlist differs"):
            shortlist_canary(self.root, self.plan_id, snapshot_root=self.snapshots)


if __name__ == "__main__":
    unittest.main()
