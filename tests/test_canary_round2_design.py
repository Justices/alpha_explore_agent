"""Offline second-round research design, pinned to failed canary and exact catalogs."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from io import StringIO
from contextlib import redirect_stdout

from alpha_research.cli_canary import FIELD, SCOPE, run_canary_command
from alpha_research.cli_canary_diagnose import diagnose_canary
from alpha_research.cli_canary_design import design_canary
from alpha_research.cli_canary_review import review_canary
from alpha_research.state.store import atomic_write_json, file_sha256
from tests.test_canary_review import CanaryReviewTests


class CanaryRoundTwoDesignTests(unittest.TestCase):
    def setUp(self):
        # Reuse exact offline platform receipt + fingerprint fixtures.
        previous = CanaryReviewTests("test_missing_ra_does_not_get_assumed_pass")
        previous.setUp()
        self.addCleanup(previous.doCleanups)
        self.legacy = previous
        self.root = previous.root
        self.plan_id = previous.plan_id
        previous._save_platform_check_rows([
            {"name": "CONCENTRATED_WEIGHT", "result": "FAIL"},
            {"name": "HIGH_TURNOVER", "result": "FAIL"},
            {"name": "LOW_2Y_SHARPE", "result": "FAIL"},
            {"name": "LOW_FITNESS", "result": "FAIL"},
            {"name": "LOW_SHARPE", "result": "FAIL"},
            {"name": "LOW_SUB_UNIVERSE_SHARPE", "result": "PASS"},
            {"name": "LOW_TURNOVER", "result": "PASS"},
        ])
        review_canary(self.root, self.plan_id)
        diagnose_canary(self.root, self.plan_id)
        self.snapshots = self.root / "knowledge"
        self.snapshots.mkdir()
        self.fields = [
            {"id": FIELD, "type": "MATRIX", "dataset": {"id": "analyst10"},
             "description": "Analyst EBITDA revision ratio",
             "coverage": 0.8, "zeroFraction": 0.2},
        ]
        self.ops = [
            {"name": "rank", "scope": ["REGULAR"], "definition": "rank(x)"},
            {"name": "ts_mean", "scope": ["REGULAR"], "definition": "ts_mean(x, d)"},
            {"name": "ts_zscore", "scope": ["REGULAR"], "definition": "ts_zscore(x, d)"},
            {"name": "ts_delta", "scope": ["REGULAR"], "definition": "ts_delta(x, d)"},
        ]
        self._save_catalogs()
        canary_path = previous.canary_path
        record = json.loads(canary_path.read_text(encoding="utf-8"))
        record["catalogs"] = {"field_count": len(self.fields),
                              "regular_operator_count": len(self.ops)}
        atomic_write_json(canary_path, record)

    def _save_catalogs(self, *, namespace="offline-namespace"):
        case = (self.root / "state" / "platform" / "cases" /
                ("canary_" + self.plan_id))
        stamp = datetime.now(timezone.utc).isoformat()
        field_rel = Path("data_fields/USA/1/TOP3000/analyst10.json")
        ops_rel = Path("operator/operators.json")
        for rel, ref, rows, meta, key in (
                (field_rel, Path("fields/USA/1/TOP3000/analyst10.ref.json"),
                 self.fields, {
                     "region": "USA", "delay": 1, "universe": "TOP3000",
                     "instrument_type": "EQUITY", "dataset": "analyst10",
                     "source": "https://api.worldquantbrain.com/data-fields",
                 }, "fields"),
                (ops_rel, Path("catalogs/operators.ref.json"),
                 self.ops, {"source": "https://api.worldquantbrain.com/operators"},
                 "operators")):
            path = self.snapshots / rel
            atomic_write_json(path, {
                "metadata": {**meta, "complete": True, "count": len(rows),
                             "account_namespace": namespace, "fetched_at": stamp},
                key: rows,
            })
            atomic_write_json(case / ref, {"snapshot_ref": str(path),
                                            "sha256": file_sha256(path),
                                            "fetched_at": stamp})

    def test_three_candidates_have_real_signature_pass_and_are_not_authorized(self):
        out = design_canary(self.root, self.plan_id, snapshot_root=self.snapshots)
        self.assertEqual(out["status"], "DESIGNED_OFFLINE")
        self.assertEqual(out["counts"], {
            "total": 3, "static_pass": 3, "static_blocked": 0})
        self.assertEqual(out["failure_signatures"],
                         ["S_SHARPE", "S_FITNESS", "S_TURNOVER_HI",
                          "S_CONCENTRATED", "S_2Y"])
        self.assertTrue(out["concentration_risk"]["no_concentration_remedy_assumed"])
        self.assertTrue(out["catalog_evidence"]["source_account_verified_equal"])
        self.assertEqual(out["original_field_facts"]["zeroFraction"], 0.2)
        for row in out["proposals"]:
            self.assertEqual(row["static_status"], "STATIC_PASS")
            self.assertEqual(row["static_validation"]["warnings"], [])
            self.assertEqual(row["field_ids"], [FIELD])
            self.assertFalse(row["simulation_authorized"])
            self.assertFalse(row["submission_authorized"])
            self.assertFalse(row["candidate_eligible"])
        self.assertFalse(out["simulation_invoked"])
        self.assertEqual(design_canary(
            self.root, self.plan_id, snapshot_root=self.snapshots)["status"],
            "DESIGNED_OFFLINE")

    def test_missing_live_signature_does_not_admit_expression(self):
        self.ops[2]["definition"] = None
        self._save_catalogs()
        out = design_canary(self.root, self.plan_id, snapshot_root=self.snapshots)
        blocked = [x for x in out["proposals"] if x["static_status"] == "STATIC_BLOCKED"]
        self.assertEqual([x["proposal_id"] for x in blocked], ["R2_RELATIVE_20"])
        self.assertEqual(blocked[0]["static_validation"]["warnings"][0]["code"],
                         "W_OPERATOR_SIGNATURE_UNVERIFIED")
        self.assertEqual(out["counts"]["static_pass"], 2)

    def test_unknown_operator_does_not_admit_expression(self):
        self.ops = [x for x in self.ops if x["name"] != "ts_delta"]
        self._save_catalogs()
        record = json.loads(self.legacy.canary_path.read_text(encoding="utf-8"))
        record["catalogs"]["regular_operator_count"] = len(self.ops)
        atomic_write_json(self.legacy.canary_path, record)
        out = design_canary(self.root, self.plan_id, snapshot_root=self.snapshots)
        self.assertEqual(out["proposals"][2]["static_status"], "STATIC_BLOCKED")
        codes = [x["code"] for x in out["proposals"][2]["static_validation"]["errors"]]
        self.assertIn("E_UNKNOWN_OPERATOR", codes)

    def test_changed_snapshot_without_reference_update_blocks(self):
        self.fields[0]["coverage"] = 0.4
        path = self.snapshots / "data_fields/USA/1/TOP3000/analyst10.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["fields"] = self.fields
        atomic_write_json(path, data)
        with self.assertRaisesRegex(Exception, "differs from original Canary reference"):
            design_canary(self.root, self.plan_id, snapshot_root=self.snapshots)

    def test_other_account_snapshot_blocks(self):
        path = self.snapshots / "operator/operators.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["metadata"]["account_namespace"] = "another-account"
        atomic_write_json(path, data)
        ref = self.root / "state" / "platform" / "cases" / (
            "canary_" + self.plan_id) / "catalogs/operators.ref.json"
        r = json.loads(ref.read_text(encoding="utf-8"))
        r["sha256"] = file_sha256(path)
        atomic_write_json(ref, r)
        with self.assertRaisesRegex(Exception, "account identities differ"):
            design_canary(self.root, self.plan_id, snapshot_root=self.snapshots)

    def test_modified_prior_diagnosis_is_rejected(self):
        path = self.root / "state" / "canary" / (self.plan_id + "_diagnosis.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["failed_ra_checks"] = []
        atomic_write_json(path, data)
        with self.assertRaisesRegex(Exception, "existing diagnosis differs"):
            design_canary(self.root, self.plan_id, snapshot_root=self.snapshots)

    def test_no_network_cli_and_repeatability(self):
        args = SimpleNamespace(command="canary-design", plan_id=self.plan_id)
        out = StringIO()
        with (patch("alpha_research.cli_canary_design.CATALOG_SNAPSHOT_ROOT",
                    self.snapshots),
              patch.dict("os.environ", {"BRAIN_CREDENTIALS_FILE": ""}),
              redirect_stdout(out)):
            rc = run_canary_command(
                args, self.root,
                gateway_factory=lambda: self.fail("Gateway must never be constructed"))
        self.assertEqual(rc, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["status"], "DESIGNED_OFFLINE")
        self.assertEqual(report["source_alpha_id"], "TESTALPHA")
        self.assertFalse(report["model_invoked"])
        self.assertFalse(report["simulation_invoked"])
        self.assertFalse(report["alpha_submission_invoked"])


if __name__ == "__main__":
    unittest.main()
