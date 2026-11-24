"""All-offline field-substitution decisions: no model, POST, correlation, submission."""
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from alpha_research.cli_canary import FIELD, run_canary_command
from alpha_research.cli_canary_design import design_canary
from alpha_research.cli_canary_field_decision import decide_fields_canary
from alpha_research.state.store import atomic_write_json
from tests.test_canary_shortlist import RoundTwoShortlistTests


class CanaryFieldDecisionTests(unittest.TestCase):
    def setUp(self):
        fixture = RoundTwoShortlistTests(
            "test_prioritization_does_not_claim_real_performance_or_simulation_permission")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root = fixture.root
        self.plan_id = fixture.plan_id
        self.snapshots = fixture.snapshots

    def _real_shape(self):
        # Synthetic offline fixture matching user-reported catalog facts:
        # 1 source, 4 directional revision counts, 1 ratio, 1 magnitude.
        self.fixture.fields = [
            {"id": FIELD, "dataset": {"id": "analyst10"},
             "type": "MATRIX", "description": "EBITDA revision ratio to close Q1",
             "coverage": 0.2657, "dateCoverage": 1.0},
            {"id": "analyst_innovative_decrease_ebitda_q1",
             "dataset": {"id": "analyst10"}, "type": "MATRIX",
             "description": "Count of innovative downward revisions in analyst EBITDA estimates",
             "coverage": 0.2658, "dateCoverage": 1.0},
            {"id": "analyst_innovative_increase_ebitda_q1",
             "dataset": {"id": "analyst10"}, "type": "MATRIX",
             "description": "Count of innovative upward revisions in analyst EBITDA estimates",
             "coverage": 0.2658, "dateCoverage": 1.0},
            {"id": "analyst_normal_decrease_ebitda_q1",
             "dataset": {"id": "analyst10"}, "type": "MATRIX",
             "description": "Count of standard downward analyst EBITDA revisions",
             "coverage": 0.2658, "dateCoverage": 1.0},
            {"id": "analyst_normal_increase_ebitda_q1",
             "dataset": {"id": "analyst10"}, "type": "MATRIX",
             "description": "Count of standard upward analyst EBITDA revisions",
             "coverage": 0.2658, "dateCoverage": 1.0},
            {"id": "analyst_revision_ratio_to_consensus_ebitda_q1",
             "dataset": {"id": "analyst10"}, "type": "MATRIX",
             "description": "Ratio of analyst EBITDA estimate revisions to consensus",
             "coverage": 0.2657, "dateCoverage": 1.0},
            {"id": "analyst_revision_value_ebitda_q1",
             "dataset": {"id": "analyst10"}, "type": "MATRIX",
             "description": "Magnitude of analyst EBITDA estimate revision",
             "coverage": 0.2657, "dateCoverage": 1.0},
        ]
        self.fixture._save_catalogs()
        canary_path = self.fixture.previous.legacy.canary_path
        canary = json.loads(canary_path.read_text(encoding="utf-8"))
        canary["catalogs"]["field_count"] = len(self.fixture.fields)
        atomic_write_json(canary_path, canary)
        design_path = self.root / "state" / "canary" / (
            self.plan_id + "_round2_design.json")
        # Fixture has a previous synthetic design for 0.78 coverage; recreate
        # clean, never mutate genuine production evidence.
        design_path.unlink()
        design_canary(self.root, self.plan_id, snapshot_root=self.snapshots)

    def test_real_shape_no_material_gain_and_no_substitution(self):
        self._real_shape()
        result = decide_fields_canary(
            self.root, self.plan_id, snapshot_root=self.snapshots)
        self.assertEqual(result["status"],
                         "NO_COVERAGE_BENEFIT_FROM_FIELD_SUBSTITUTION")
        self.assertEqual(result["catalog_coverage_comparison"]["alternative_count"], 6)
        self.assertEqual(result["catalog_coverage_comparison"]["max_coverage"], 0.2658)
        self.assertEqual(result["catalog_coverage_comparison"]["max_absolute_gain"], 0.0001)
        self.assertEqual(result["catalog_coverage_comparison"]["max_gain_percentage_points"], 0.01)
        self.assertFalse(result["catalog_coverage_comparison"]["materiality_heuristic_is_platform_rule"])
        self.assertEqual(result["semantic_family_counts"], {
            "COUNT_DOWNWARD_ONLY": 2, "COUNT_UPWARD_ONLY": 2,
            "RATIO_TO_CONSENSUS": 1,
            "REVISION_VALUE_UNVERIFIED_UNITS": 1})
        self.assertEqual(result["source_field"]["catalog_coverage"], 0.2657)
        self.assertEqual(result["source_field"]["date_coverage"], 1.0)
        self.assertTrue(result["catalog_coverage_comparison"][
            "date_coverage_is_not_cross_sectional_nonnull_rate"])
        self.assertFalse(result["research_decision"]["replacement_approved"])
        self.assertFalse(result["research_decision"]["retest_all_12_expressions"])
        self.assertFalse(result["research_decision"]["concentration_mechanism_identified"])
        self.assertFalse(result["simulation_authorized"])
        self.assertFalse(result["ready_for_simulation"])
        self.assertFalse(result["correlation_allowed"])
        self.assertEqual(len(result["outstanding_evidence"]), 4)
        self.assertEqual(decide_fields_canary(
            self.root, self.plan_id, snapshot_root=self.snapshots)["status"],
            "NO_COVERAGE_BENEFIT_FROM_FIELD_SUBSTITUTION")

    def test_larger_coverage_gain_only_triggers_manual_semantics_review(self):
        result = decide_fields_canary(
            self.root, self.plan_id, snapshot_root=self.snapshots)
        self.assertEqual(result["status"], "FIELD_SUBSTITUTION_REVIEW_REQUIRED")
        self.assertGreater(
            result["catalog_coverage_comparison"]["max_absolute_gain"], 0.05)
        self.assertFalse(result["research_decision"]["replacement_approved"])
        self.assertFalse(result["simulation_authorized"])
        self.assertTrue(all(not x["automatic_substitution_allowed"]
                            for x in result["field_comparisons"]))

    def test_existing_report_cannot_be_edited_to_grant_simulation(self):
        self._real_shape()
        result = decide_fields_canary(
            self.root, self.plan_id, snapshot_root=self.snapshots)
        p = Path(result["decision_path"])
        changed = json.loads(p.read_text(encoding="utf-8"))
        changed["simulation_authorized"] = True
        atomic_write_json(p, changed)
        with self.assertRaisesRegex(Exception, "existing field-substitution decision differs"):
            decide_fields_canary(self.root, self.plan_id, snapshot_root=self.snapshots)

    def test_inconsistent_upstream_review_refuses_decision(self):
        self._real_shape()
        result = decide_fields_canary(
            self.root, self.plan_id, snapshot_root=self.snapshots)
        p = self.root / "state" / "canary" / (
            self.plan_id + "_round3_field_review.json")
        changed = json.loads(p.read_text(encoding="utf-8"))
        changed["source_field"]["coverage"] = 0.9
        atomic_write_json(p, changed)
        with self.assertRaisesRegex(Exception, "previous Round 3 field audit differs"):
            decide_fields_canary(self.root, self.plan_id, snapshot_root=self.snapshots)
        self.assertFalse(json.loads(Path(result["decision_path"]).read_text(
            encoding="utf-8"))["simulation_authorized"])

    def test_no_network_cli_no_credential_needed(self):
        self._real_shape()
        args = SimpleNamespace(command="canary-field-decision", plan_id=self.plan_id)
        out = StringIO()
        with (patch("alpha_research.cli_canary_field_decision.CATALOG_SNAPSHOT_ROOT",
                    self.snapshots),
              patch.dict("os.environ", {"BRAIN_CREDENTIALS_FILE": ""}),
              redirect_stdout(out)):
            result = run_canary_command(
                args, self.root,
                gateway_factory=lambda: self.fail("No Gateway permitted"))
        self.assertEqual(result, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"],
                         "NO_COVERAGE_BENEFIT_FROM_FIELD_SUBSTITUTION")
        self.assertFalse(payload["model_invoked"])
        self.assertFalse(payload["simulation_invoked"])
        self.assertFalse(payload["alpha_submission_invoked"])


if __name__ == "__main__":
    unittest.main()
