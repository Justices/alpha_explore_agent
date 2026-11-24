"""Round-three offline field review is bounded by trusted catalogs, never POSTs."""
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from alpha_research.cli_canary import FIELD, run_canary_command
from alpha_research.cli_canary_field_review import (
    _semantic_facts, _static_expression, review_fields_canary,
)
from alpha_research.state.store import atomic_write_json
from tests.test_canary_shortlist import RoundTwoShortlistTests


class CanaryFieldReviewTests(unittest.TestCase):
    def setUp(self):
        fixture = RoundTwoShortlistTests(
            "test_prioritization_does_not_claim_real_performance_or_simulation_permission")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root = fixture.root
        self.plan_id = fixture.plan_id
        self.snapshots = fixture.snapshots

    def test_verified_field_and_static_alternatives_are_never_promoted(self):
        result = review_fields_canary(
            self.root, self.plan_id, snapshot_root=self.snapshots)
        self.assertEqual(result["status"], "FIELDS_REVIEW_REQUIRED")
        self.assertEqual(result["source_alpha_id"], "TESTALPHA")
        self.assertEqual(result["source_field"]["coverage"], 0.2657)
        self.assertFalse(result["source_field"]["weight_distribution_inspected"])
        self.assertFalse(result["source_field"]["concentration_mechanism_determined"])
        self.assertEqual(result["counts"], {
            "alternatives_shown": 2,
            "expressions_checked": 4,
            "static_pass": 4,
            "static_blocked": 0,
        })
        self.assertEqual(
            [x["field_id"] for x in result["alternatives"]],
            ["analyst_revision_count_ebitda_q1", "analyst_innovative_increase_ebitda_q1"])
        self.assertEqual(result["alternatives"][0]["catalog_facts"]["coverage"], 0.78)
        self.assertEqual(result["alternatives"][0]["semantics"]["catalog_measure_clue"],
                         "revision_count")
        self.assertEqual(result["alternatives"][1]["semantics"]["directionality_clue"],
                         "upward_only")
        for field in result["alternatives"]:
            self.assertFalse(field["same_measure_as_source_observed"])
            self.assertTrue(field["requires_manual_field_approval"])
            self.assertFalse(field["ready_for_simulation"])
            for expr in field["candidate_expressions"]:
                self.assertEqual(expr["status"], "STATIC_PASS")
                self.assertFalse(expr["simulation_authorized"])
                self.assertFalse(expr["substitution_approved"])
        self.assertFalse(result["simulation_invoked"])
        self.assertFalse(result["model_invoked"])
        self.assertFalse(result["correlation_checked"])
        self.assertFalse(result["candidate_eligible"])
        self.assertEqual(review_fields_canary(
            self.root, self.plan_id, snapshot_root=self.snapshots)["status"],
            "FIELDS_REVIEW_REQUIRED")

    def test_unverifiable_signature_is_blocked_even_if_ast_parses(self):
        field = "analyst_revision_count_ebitda_q1"
        fields = [{"id": field, "dataset": {"id": "analyst10"}, "type": "MATRIX"}]
        ops = [{"name": "rank", "definition": "rank(x)", "scope": ["REGULAR"]},
               {"name": "ts_mean", "definition": None, "scope": ["REGULAR"]}]
        single = _static_expression(field, fields, ops, {"rank", "ts_mean"})
        smooth = _static_expression(field, fields, ops, {"rank", "ts_mean"},
                                    smoothing=True)
        self.assertEqual(single["status"], "STATIC_PASS")
        self.assertEqual(smooth["status"], "STATIC_BLOCKED")
        self.assertEqual(smooth["warnings"][0]["code"],
                         "W_OPERATOR_SIGNATURE_UNVERIFIED")
        self.assertFalse(smooth["simulation_authorized"])

    def test_semantics_are_only_clues_never_equivalence(self):
        ratio = _semantic_facts(FIELD, "Ratio of analyst EBITDA estimate revisions")
        count = _semantic_facts(
            "analyst_innovative_increase_ebitda_q1",
            "Count of innovative upward revisions in EBITDA")
        self.assertEqual(ratio["catalog_measure_clue"], "revision_ratio")
        self.assertEqual(count["catalog_measure_clue"], "revision_count")
        self.assertEqual(count["forecast_quarter_clues"], ["q1"])
        self.assertEqual(count["economic_semantic_equivalence_to_source"], "UNVERIFIED")

    def test_prior_field_review_is_immutable_and_source_tampering_rejected(self):
        result = review_fields_canary(
            self.root, self.plan_id, snapshot_root=self.snapshots)
        path = Path(result["field_review_path"])
        value = json.loads(path.read_text(encoding="utf-8"))
        value["simulation_authorized"] = True
        atomic_write_json(path, value)
        with self.assertRaisesRegex(Exception, "previous Round 3 field audit differs"):
            review_fields_canary(self.root, self.plan_id, snapshot_root=self.snapshots)

    def test_changed_catalog_cannot_overwrite_field_review(self):
        result = review_fields_canary(
            self.root, self.plan_id, snapshot_root=self.snapshots)
        catalog = self.snapshots / "data_fields/USA/1/TOP3000/analyst10.json"
        body = json.loads(catalog.read_text(encoding="utf-8"))
        body["fields"][0]["coverage"] = 0.9
        atomic_write_json(catalog, body)
        with self.assertRaisesRegex(Exception, "differs from original Canary reference"):
            review_fields_canary(self.root, self.plan_id, snapshot_root=self.snapshots)
        saved = json.loads(Path(result["field_review_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["source_field"]["coverage"], 0.2657)

    def test_cli_one_step_creates_missing_shortlist_without_gateway(self):
        args = SimpleNamespace(command="canary-field-review", plan_id=self.plan_id)
        out = StringIO()
        with (patch("alpha_research.cli_canary_field_review.CATALOG_SNAPSHOT_ROOT",
                    self.snapshots),
              patch.dict("os.environ", {"BRAIN_CREDENTIALS_FILE": ""}),
              redirect_stdout(out)):
            code = run_canary_command(
                args, self.root,
                gateway_factory=lambda: self.fail("Gateway must not be constructed"))
        self.assertEqual(code, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["status"], "FIELDS_REVIEW_REQUIRED")
        self.assertFalse(payload["simulation_invoked"])
        self.assertFalse(payload["alpha_submission_invoked"])
        previous = (self.root / "state" / "canary" /
                    (self.plan_id + "_round2_shortlist.json"))
        self.assertTrue(previous.is_file())


if __name__ == "__main__":
    unittest.main()
