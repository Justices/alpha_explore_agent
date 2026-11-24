"""Canary distribution audit: strict exported evidence and no implied platform actions."""
import csv
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from alpha_research.cli_canary import FIELD, SCOPE, run_canary_command
from alpha_research.cli_canary_distribution import audit_distribution
from alpha_research.state.store import atomic_write_json, file_sha256
from tests.test_canary_field_decision import CanaryFieldDecisionTests


class CanaryDistributionTests(unittest.TestCase):
    def setUp(self):
        fixture = CanaryFieldDecisionTests(
            "test_real_shape_no_material_gain_and_no_substitution")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root = fixture.root
        self.plan_id = fixture.plan_id
        self.snapshots = fixture.snapshots
        self.inputs = self.root / "evidence" / "canary" / self.plan_id

    def _export(self, *, claims=None, universe=None, signal=None, weights=None):
        self.inputs.mkdir(parents=True, exist_ok=True)
        universe = universe if universe is not None else [
            ["2026-11-20", "A", "Tech"], ["2026-11-20", "B", "Tech"],
            ["2026-11-20", "C", "Health"], ["2026-11-23", "A", "Tech"],
            ["2026-11-23", "B", "Tech"], ["2026-11-23", "C", "Health"],
        ]
        signal = signal if signal is not None else [
            ["2026-11-20", "A", "1"], ["2026-11-20", "B", ""],
            ["2026-11-20", "C", "4"], ["2026-11-23", "A", "2"],
            ["2026-11-23", "B", ""], ["2026-11-23", "C", "4"],
        ]
        weights = weights if weights is not None else [
            ["2026-11-20", "A", "0.5"], ["2026-11-20", "B", "0.2"],
            ["2026-11-20", "C", "-0.3"], ["2026-11-23", "A", "0.2"],
            ["2026-11-23", "B", "0.2"], ["2026-11-23", "C", "-0.6"],
        ]
        expected = {
            "universe.csv": (["date", "instrument_id", "sector"], universe),
            "signal.csv": (["date", "instrument_id", "field_value"], signal),
            "weights.csv": (["date", "instrument_id", "portfolio_weight"], weights),
        }
        digests = {}
        for name, (columns, rows) in expected.items():
            path = self.inputs / name
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(columns)
                writer.writerows(rows)
            digests[name] = file_sha256(path)
        atomic_write_json(self.inputs / "evidence_manifest.json", {
            "schema_version": 1, "plan_id": self.plan_id,
            "alpha_id": "TESTALPHA", "field_id": FIELD,
            "scope": SCOPE, "source_type": "LOCAL_EXPORT",
            "completeness": claims or {
                "universe_complete": True,
                "signal_exhaustive": True,
                "weights_exhaustive": True,
            },
            "source_descriptions": {k: "synthetic offline fixture" for k in expected},
            "files_sha256": digests,
        })

    def _audit(self):
        return audit_distribution(
            self.root, self.plan_id, evidence_dir=self.inputs,
            snapshot_root=self.snapshots)

    def test_no_data_has_explicit_missing_requirements_and_zero_metrics(self):
        result = self._audit()
        self.assertEqual(result["status"], "DISTRIBUTION_EVIDENCE_MISSING")
        self.assertEqual(result["computed_metrics"], None)
        self.assertCountEqual(result["missing_inputs"],
                              ["universe.csv", "signal.csv", "weights.csv",
                               "evidence_manifest.json"])
        self.assertFalse(result["source_provenance_verified"])
        self.assertFalse(result["simulation_authorized"])

    def test_dated_effective_coverage_sectors_weights_and_updates(self):
        self._export()
        result = self._audit()
        self.assertEqual(result["status"],
                         "DISTRIBUTION_METRICS_COMPUTED_PROVENANCE_REVIEW_REQUIRED")
        daily = result["computed_metrics"]["dates"]
        self.assertEqual(len(daily), 2)
        self.assertEqual(daily[0]["nonnull_fraction_of_exported_universe"], 0.66666667)
        self.assertEqual(daily[0]["portfolio"]["gross_weight_hhi"], 0.38)
        self.assertEqual(daily[0]["portfolio"]["effective_gross_holdings"], 2.63157895)
        self.assertEqual(daily[1]["portfolio"]["top5_gross_weight_share"], 1.0)
        updates = result["computed_metrics"]["signal_changes_between_exported_dates"]
        self.assertEqual(updates[0]["nonnull_pairs"], 2)
        self.assertEqual(updates[0]["changed_pairs"], 1)
        self.assertEqual(updates[0]["change_fraction_of_comparable_pairs"], 0.5)
        by_sector = {r["sector"]: r for r in result["computed_metrics"]["by_sector"]}
        self.assertEqual(by_sector["Tech"]["nonnull_fraction"], 0.5)
        self.assertEqual(by_sector["Health"]["nonnull_fraction"], 1.0)
        self.assertFalse(result["computed_metrics"]["actual_BRAIN_distribution_equivalence_verified"])
        self.assertFalse(result["computed_metrics"]["concentrated_weight_RA_root_cause_established"])
        self.assertFalse(result["source_provenance_verified"])
        self.assertFalse(result["simulation_invoked"])
        self.assertFalse(result["candidate_eligible"])
        self.assertEqual(Path(result["distribution_report_path"]).is_file(), True)
        self.assertEqual(self._audit()["distribution_report_path"],
                         result["distribution_report_path"])

    def test_nonexhaustive_evidence_is_never_called_actual_universe_or_full_portfolio(self):
        self._export(claims={
            "universe_complete": False, "signal_exhaustive": False,
            "weights_exhaustive": False})
        result = self._audit()
        measured = result["computed_metrics"]
        self.assertEqual(measured["coverage_interpretation"],
                         "SAMPLE_ONLY_UNKNOWN_TRUE_UNIVERSE_COVERAGE")
        self.assertEqual(measured["portfolio_interpretation"],
                         "SAMPLE_ONLY_NO_PORTFOLIO_CONCENTRATION_INFERENCE")
        self.assertTrue(all(r["portfolio"] is None for r in measured["dates"]))
        self.assertFalse(result["simulation_authorized"])

    def test_hash_tampering_and_nonfinite_values_are_rejected(self):
        self._export()
        with (self.inputs / "signal.csv").open("a", encoding="utf-8") as handle:
            handle.write("2026-11-24,A,1\n")
        with self.assertRaisesRegex(Exception, "hash differs"):
            self._audit()
        self._export(signal=[
            ["2026-11-20", "A", "NaN"], ["2026-11-20", "B", ""],
            ["2026-11-20", "C", "4"], ["2026-11-23", "A", "2"],
            ["2026-11-23", "B", ""], ["2026-11-23", "C", "4"],
        ])
        with self.assertRaisesRegex(Exception, "NaN or infinite"):
            self._audit()

    def test_duplicate_instrument_date_and_false_exhaustive_claim_rejected(self):
        self._export(signal=[
            ["2026-11-20", "A", "1"], ["2026-11-20", "A", "2"],
            ["2026-11-20", "C", "4"], ["2026-11-23", "A", "2"],
            ["2026-11-23", "B", ""], ["2026-11-23", "C", "4"],
        ])
        with self.assertRaisesRegex(Exception, "duplicate instrument-date"):
            self._audit()
        self._export(signal=[
            ["2026-11-20", "A", "1"],
            ["2026-11-20", "C", "4"], ["2026-11-23", "A", "2"],
            ["2026-11-23", "B", ""], ["2026-11-23", "C", "4"],
        ])
        with self.assertRaisesRegex(Exception, "rows are missing"):
            self._audit()

    def test_wrong_alpha_or_scope_blocked(self):
        self._export()
        p = self.inputs / "evidence_manifest.json"
        manifest = json.loads(p.read_text(encoding="utf-8"))
        manifest["alpha_id"] = "UNKNOWNALPHA"
        atomic_write_json(p, manifest)
        with self.assertRaisesRegex(Exception, "identity or scope"):
            self._audit()

    def test_cli_no_gateway_and_report_can_never_be_overwritten(self):
        self._export()
        args = SimpleNamespace(command="canary-distribution-audit",
                               plan_id=self.plan_id, evidence_dir=self.inputs)
        output = StringIO()
        with (patch("alpha_research.cli_canary_distribution.audit_distribution",
                    wraps=lambda root, plan_id, evidence_dir: audit_distribution(
                        root, plan_id, evidence_dir=evidence_dir,
                        snapshot_root=self.snapshots)),
              patch.dict("os.environ", {"BRAIN_CREDENTIALS_FILE": ""}),
              redirect_stdout(output)):
            rc = run_canary_command(
                args, self.root,
                gateway_factory=lambda: self.fail("Gateway must not be constructed"))
        self.assertEqual(rc, 0)
        value = json.loads(output.getvalue())
        self.assertFalse(value["simulation_invoked"])
        self.assertFalse(value["alpha_submission_invoked"])
        p = Path(value["distribution_report_path"])
        record = json.loads(p.read_text(encoding="utf-8"))
        record["simulation_authorized"] = True
        atomic_write_json(p, record)
        with self.assertRaisesRegex(Exception, "report differs"):
            self._audit()


if __name__ == "__main__":
    unittest.main()
