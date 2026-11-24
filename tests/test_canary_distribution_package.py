"""Offline evidence packaging: no fabricated rows, platform calls or overwrite."""
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from alpha_research.cli_canary import run_canary_command
from alpha_research.cli_canary_distribution_package import package_distribution
from alpha_research.cli_parser import build_parser
from alpha_research.state.store import file_sha256
from tests.test_canary_distribution import CanaryDistributionTests


class CanaryDistributionPackageTests(unittest.TestCase):
    def setUp(self):
        fixture = CanaryDistributionTests(
            "test_no_data_has_explicit_missing_requirements_and_zero_metrics")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.root = fixture.root
        self.plan_id = fixture.plan_id
        self.inputs = fixture.inputs

    def _package(self, **kwargs):
        options = {
            "universe_source": "Original dated universe export",
            "signal_source": "Original analyst10 field-value export",
            "weights_source": "Original Alpha instrument-weight export",
            "snapshot_root": self.fixture.snapshots,
        }
        options.update(kwargs)
        return package_distribution(
            self.root, self.plan_id, evidence_dir=self.inputs, **options)

    def _csv_only(self, **kwargs):
        self.fixture._export(**kwargs)
        (self.inputs / "evidence_manifest.json").unlink()

    def test_missing_csv_never_creates_an_empty_manifest_or_metrics(self):
        result = self._package()
        self.assertEqual(result["status"], "DISTRIBUTION_SOURCE_CSV_MISSING")
        self.assertEqual(set(result["missing_inputs"]),
                         {"universe.csv", "signal.csv", "weights.csv"})
        self.assertIsNone(result["computed_metrics"])
        self.assertFalse(result["manifest_created"])
        self.assertFalse((self.inputs / "evidence_manifest.json").exists())
        self.assertFalse(result["simulation_authorized"])

    def test_package_generated_manifest_is_idempotent_and_runs_offline_audit(self):
        self._csv_only()
        flags = {"universe_complete": True,
                 "signal_exhaustive": True,
                 "weights_exhaustive": True}
        result = self._package(**flags)
        self.assertEqual(result["status"],
                         "DISTRIBUTION_METRICS_COMPUTED_PROVENANCE_REVIEW_REQUIRED")
        self.assertTrue(result["manifest_created"])
        self.assertEqual(result["computed_metrics"]["date_count"], 2)
        self.assertFalse(result["source_provenance_verified"])
        self.assertFalse(result["simulation_authorized"])
        self.assertFalse(result["alpha_submission_invoked"])
        self.assertTrue(Path(result["distribution_report_path"]).is_file())
        manifest = json.loads(
            (self.inputs / "evidence_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["alpha_id"], "TESTALPHA")
        self.assertEqual(manifest["completeness"], flags)
        for name, digest in manifest["files_sha256"].items():
            self.assertEqual(file_sha256(self.inputs / name), digest)
        repeat = self._package(**flags)
        self.assertFalse(repeat["manifest_created"])
        self.assertEqual(repeat["distribution_report_path"],
                         result["distribution_report_path"])

    def test_default_attestations_cannot_claim_complete_portfolio(self):
        self._csv_only()
        result = self._package()
        self.assertEqual(
            result["computed_metrics"]["coverage_interpretation"],
            "SAMPLE_ONLY_UNKNOWN_TRUE_UNIVERSE_COVERAGE")
        self.assertTrue(all(
            row["portfolio"] is None
            for row in result["computed_metrics"]["dates"]))
        self.assertFalse(result["source_provenance_verified"])

    def test_changed_csv_requires_a_new_immutable_manifest_not_overwrite(self):
        self._csv_only()
        self._package()
        with (self.inputs / "signal.csv").open("a", encoding="utf-8") as stream:
            stream.write("\n")
        with self.assertRaisesRegex(Exception, "manifest differs"):
            self._package()

    def test_false_exhaustive_claim_is_rejected_before_manifest_creation(self):
        self._csv_only(signal=[
            ["2026-11-20", "A", "1"], ["2026-11-20", "C", "4"],
            ["2026-11-23", "A", "2"], ["2026-11-23", "B", ""],
            ["2026-11-23", "C", "4"],
        ])
        with self.assertRaisesRegex(Exception, "rows are missing"):
            self._package(signal_exhaustive=True)
        self.assertFalse((self.inputs / "evidence_manifest.json").exists())

    def test_source_descriptions_and_attestations_must_be_explicit(self):
        self._csv_only()
        with self.assertRaisesRegex(Exception, "source description"):
            self._package(weights_source="   ")
        with self.assertRaisesRegex(Exception, "boolean"):
            self._package(weights_exhaustive=1)
        self.assertFalse((self.inputs / "evidence_manifest.json").exists())

    def test_command_routing_does_not_construct_gateway(self):
        self._csv_only()
        args = SimpleNamespace(
            command="canary-distribution-package", plan_id=self.plan_id,
            evidence_dir=self.inputs,
            universe_source="Original dated universe export",
            signal_source="Original field export",
            weights_source="Original portfolio export",
            universe_complete=False, signal_exhaustive=False,
            weights_exhaustive=False,
        )
        output = StringIO()
        with (patch(
                "alpha_research.cli_canary_distribution_package.package_distribution",
                wraps=lambda root, plan_id, **opts:
                    package_distribution(root, plan_id,
                                         snapshot_root=self.fixture.snapshots,
                                         **opts)),
              redirect_stdout(output)):
            rc = run_canary_command(
                args, self.root,
                gateway_factory=lambda: self.fail("BRAIN Gateway must remain unused"))
        self.assertEqual(rc, 0)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["simulation_invoked"])
        self.assertFalse(payload["alpha_tags_modified"])
        self.assertFalse(payload["candidate_eligible"])

    def test_parser_defaults_are_conservative(self):
        args = build_parser().parse_args([
            "canary-distribution-package", "--plan-id", self.plan_id,
            "--universe-source", "actual universe export",
            "--signal-source", "actual signal export",
            "--weights-source", "actual weights export",
        ])
        self.assertFalse(args.universe_complete)
        self.assertFalse(args.signal_exhaustive)
        self.assertFalse(args.weights_exhaustive)


if __name__ == "__main__":
    unittest.main()
