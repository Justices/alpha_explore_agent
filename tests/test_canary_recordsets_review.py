"""Offline recordset interpretation remains separate from raw instrument evidence."""
import json
import unittest
from pathlib import Path

from alpha_research.cli_canary_recordsets_review import (
    _numeric_series, review_canary_recordsets,
)
from alpha_research.cli_parser import build_parser
from alpha_research.state.store import atomic_write_json
from tests.test_canary_recordsets import (
    CanaryRecordsetsTests, FakeGetOnlyClient,
)


class CanaryRecordsetsReviewTests(unittest.TestCase):
    def setUp(self):
        fixture = CanaryRecordsetsTests(
            "test_get_only_capture_pins_alpha_and_retains_unaltered_schema")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.f = fixture
        self.root = fixture.root
        self.plan_id = fixture.plan_id

    def _evidence(self, data=None):
        default = {
            "coverage": {
                "schema": {"name": "coverage", "properties": [
                    {"name": "date", "type": "date"},
                    {"name": "instruments", "type": "integer"},
                ]},
                "records": [["2026-11-20", 100], ["2026-11-23", 101]],
            },
            "turnover": {
                "schema": {"name": "turnover", "properties": [
                    {"name": "date", "type": "date"},
                    {"name": "turnover", "type": "percent"},
                ]},
                "records": [["2026-11-20", 0.31], ["2026-11-23", 0.41]],
            },
            "coverage-by-sector": {
                "schema": {"name": "coverage-by-sector", "properties": [
                    {"name": "series", "type": "string"},
                    {"name": "technology", "type": "decimal"},
                ]},
                "records": [["coverage", 0.5]],
            },
        }
        result = self.f._observe(FakeGetOnlyClient(recordsets=data or default))
        return Path(result["recordset_evidence_path"])

    def _review(self, evidence_path):
        return review_canary_recordsets(
            self.root, self.plan_id, evidence_path=evidence_path,
            snapshot_root=self.f.f.snapshots)

    def test_pinned_typed_coverage_and_turnover_are_not_field_coverage(self):
        evidence = self._evidence()
        result = self._review(evidence)
        self.assertEqual(result["status"],
                         "ALPHA_RECORDSET_NUMERIC_EVIDENCE_REVIEWED_OFFLINE")
        coverage = result["metrics"]["coverage"]
        turnover = result["metrics"]["turnover"]
        self.assertEqual(coverage["status"], "NUMERIC_SERIES_REVIEWED")
        self.assertEqual(coverage["mean_raw"], 100.5)
        self.assertFalse(coverage["coverage_fraction_inferred"])
        self.assertFalse(coverage["source_field_coverage_inferred"])
        self.assertEqual(turnover["raw_schema_type"], "percent")
        self.assertFalse(turnover["unit_converted"])
        self.assertEqual(result["metrics"]["coverage-by-sector"]["status"],
                         "GROUP_SCHEMA_REVIEW_REQUIRED")
        self.assertFalse(result["concentration_root_cause_established"])
        self.assertFalse(result["candidate_eligible"])
        self.assertEqual(
            result["recordset_review_path"],
            self._review(evidence)["recordset_review_path"])

    def test_unavailable_recordset_index_yields_negative_review_not_generic_block(self):
        from tests.test_canary_recordsets import FakeResponse

        class IndexForbidden(FakeGetOnlyClient):
            def request(self, method, path):
                if path.endswith("/recordsets"):
                    self.paths.append(path)
                    return FakeResponse(403)
                return super().request(method, path)

        readback = self.f._observe(IndexForbidden())
        self.assertEqual(readback["status"], "ALPHA_RECORDSETS_UNAVAILABLE")
        reviewed = self._review(Path(readback["recordset_evidence_path"]))
        self.assertEqual(reviewed["status"], "ALPHA_RECORDSETS_UNAVAILABLE_OFFLINE")
        self.assertEqual(reviewed["recordset_catalog"]["http_status"], 403)
        self.assertFalse(reviewed["recordset_provenance_verified"])
        self.assertFalse(reviewed["simulation_authorized"])
        self.assertFalse(reviewed["candidate_eligible"])
        self.assertEqual(reviewed["metrics"], {})
        self.assertEqual(
            reviewed["recordset_review_path"],
            self._review(Path(readback["recordset_evidence_path"]))[
                "recordset_review_path"])

    def test_empty_listed_catalog_yields_negative_review(self):
        readback = self.f._observe(FakeGetOnlyClient(recordsets={}))
        self.assertEqual(readback["status"], "ALPHA_RECORDSETS_UNAVAILABLE")
        reviewed = self._review(Path(readback["recordset_evidence_path"]))
        self.assertEqual(reviewed["status"], "ALPHA_RECORDSETS_UNAVAILABLE_OFFLINE")
        self.assertEqual(reviewed["recordset_catalog"]["status"], "AVAILABLE")
        self.assertTrue(all(
            value == "NOT_LISTED" for value in
            reviewed["recordset_read_statuses"].values()))

    def test_scope_and_revision_conflicts_are_explicitly_distinguished(self):
        from alpha_research.cli_canary_recordsets import _fingerprint
        evidence = self._evidence()
        envelope = json.loads(evidence.read_text(encoding="utf-8"))
        envelope["report"]["source_field_decision_sha256"] = "0" * 64
        updated = self.root / "state" / "canary" / (
            self.plan_id + "_recordsets_" + _fingerprint(envelope)[:16] + ".json")
        atomic_write_json(updated, envelope)
        with self.assertRaisesRegex(Exception, "different field decision revision"):
            self._review(updated)

    def test_missing_date_and_nonfinite_metric_cannot_be_interpreted(self):
        missing_date = {
            "schema": {"name": "coverage", "properties": [
                {"name": "instruments", "type": "integer"}]},
            "records": [[100]],
        }
        self.assertEqual(
            _numeric_series("coverage", missing_date, "instruments")["status"],
            "SCHEMA_UNRESOLVED")
        invalid = {
            "schema": {"name": "turnover", "properties": [
                {"name": "date", "type": "date"},
                {"name": "turnover", "type": "percent"},
            ]},
            "records": [["2026-11-20", float("inf")]],
        }
        self.assertEqual(
            _numeric_series("turnover", invalid, "turnover")["reason"],
            "INVALID_METRIC_VALUE")

    def test_recordset_evidence_byte_change_is_rejected(self):
        evidence = self._evidence()
        data = json.loads(evidence.read_text(encoding="utf-8"))
        data["payloads"]["coverage"]["records"][0][1] = 999999
        atomic_write_json(evidence, data)
        with self.assertRaisesRegex(Exception, "fingerprint"):
            self._review(evidence)

    def test_review_rejects_out_of_scope_path(self):
        evidence = self._evidence()
        other = self.root / "recordsets-copy.json"
        other.write_bytes(evidence.read_bytes())
        with self.assertRaisesRegex(Exception, "pinned local"):
            self._review(other)

    def test_cli_parser_requires_explicit_evidence_path(self):
        with self.assertRaises(SystemExit):
            build_parser().parse_args([
                "canary-recordsets-review", "--plan-id", self.plan_id])
        args = build_parser().parse_args([
            "canary-recordsets-review", "--plan-id", self.plan_id,
            "--evidence-path", "some-evidence.json"])
        self.assertEqual(args.command, "canary-recordsets-review")


if __name__ == "__main__":
    unittest.main()
