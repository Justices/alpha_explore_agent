"""Offline provenance review proves real canary metrics without any remote calls."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from io import StringIO
from contextlib import redirect_stdout

from alpha_research.cli_canary import EXPRESSION, SCOPE, _variant, run_canary_command
from alpha_research.cli_canary_review import review_canary
from alpha_research.integrations.brain.runtime import PlatformRuntime
from alpha_research.state.store import atomic_write_json, file_sha256
from alpha_research.simulation.platform_cache import submission_key


class CanaryReviewTests(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.root = Path(td.name)
        self.plan_id = "a" * 32
        self.alpha_id = "TESTALPHA"
        self.batch_id = "20261124_120635_5ddb0243"
        self.case_key = "canary_" + self.plan_id
        self.run_id = "single_" + self.plan_id
        self.config = {"gate": {
            "sharpe_min": 1.58, "fitness_min": 1.0,
            "turnover_min": 0.05, "turnover_max": 0.30,
        }}
        atomic_write_json(self.root / "config" / "nastu_quant.json", self.config)
        self.settings = {
            **SCOPE, "neutralization": "INDUSTRY", "decay": 4,
            "truncation": 0.08, "pasteurization": "ON",
            "unit_handling": "VERIFY", "nan_handling": "OFF",
            "language": "FASTEXPR", "visualization": False,
            "test_period": "P0Y0M", "max_trade": "OFF",
        }
        self.plan_path = (self.root / "state" / "research_v1_1" /
                          "plans" / (self.plan_id + ".json"))
        atomic_write_json(self.plan_path, {
            "plan_id": self.plan_id, "status": "planned",
            "region": "USA", "coverage_complete": True, "unresolved": [],
            "requested": {"dataset_ids": ["analyst10"], "category": None,
                          "delay": 1, "universe": "TOP3000"},
            "datasets": [{"dataset_id": "analyst10", "category": "analyst",
                          "combinations": [{
                              "detail_scope": {"region": "USA", "delay": 1,
                                               "universe": "TOP3000"},
                              "scope_evidence_source": "exact_catalog"}]}],
        })
        self.canary_path = (self.root / "state" / "canary" /
                            (self.plan_id + ".json"))
        atomic_write_json(self.canary_path, {
            "schema_version": 1, "status": "COMPLETED_READBACK",
            "plan_id": self.plan_id, "plan_sha256": file_sha256(self.plan_path),
            "case_key": self.case_key, "run_id": self.run_id,
            "batch_id": self.batch_id, "alpha_id": self.alpha_id,
            "dataset_id": "analyst10", "scope": SCOPE,
            "settings": self.settings, "variants": [_variant()],
            "max_posts": 1, "model_calls": 0,
            "alpha_submission": False, "auto_tags": False,
        })
        self.platform_base = (self.root / "state" / "platform" / "cases" /
                              self.case_key / "sessions" / self.run_id /
                              "batches" / (self.batch_id + "_production"))
        self.request_path = self.platform_base.with_suffix(".request.json")
        self.receipt_path = self.platform_base.with_suffix(".admission.json")
        self.progress_path = self.platform_base.with_suffix(".progress.json")
        self.output_path = self.platform_base.with_suffix(".json")
        self.marker_path = self.platform_base.with_suffix(".post_started.json")
        self.details = {
            "id": self.alpha_id,
            "type": "REGULAR",
            "regular": {"code": EXPRESSION},
            "settings": PlatformRuntime._platform_settings(self.settings),
            "is": {"sharpe": 0.09, "fitness": 0.01,
                   "turnover": 1.791, "returns": 0.0188,
                   "drawdown": 0.6741},
        }
        atomic_write_json(self.request_path, {
            "case_key": self.case_key, "run_id": self.run_id,
            "batch_id": self.batch_id, "label": "production",
            "dataset_id": "analyst10", "settings": self.settings,
            "variants": [_variant()], "canary_no_tags": True,
        })
        atomic_write_json(self.receipt_path, {
            "dataset": "analyst10",
            "settings": PlatformRuntime._platform_settings(self.settings),
            "variants": [_variant()], "field_admissions": [{
                "passed": True, "dataset": "analyst10",
                "validation": {"verdict": "PASS", "fields": [
                    "analyst_revision_ratio_to_close_ebitda_q1"]},
            }],
        })
        atomic_write_json(self.output_path, [{
            "name": "canary_analyst_revision_ebitda_q1",
            "alpha_id": self.alpha_id, "expression": EXPRESSION,
            "success": True, "is": self.details["is"],
            "details": self.details,
        }])
        atomic_write_json(self.progress_path, {
            "status": "COMPLETE",
            "request_sha256": file_sha256(self.request_path),
            "receipt_sha256": file_sha256(self.receipt_path),
            "output_sha256": file_sha256(self.output_path),
            "resume_url": "/simulations/abc",
        })
        atomic_write_json(self.marker_path, {
            "request_sha256": file_sha256(self.request_path),
            "receipt_sha256": file_sha256(self.receipt_path),
            "submission_keys": [submission_key({
                "type": "REGULAR",
                "settings": PlatformRuntime._platform_settings(self.settings),
                "regular": EXPRESSION,
            })],
        })

    def test_offline_review_reports_metric_failure_and_never_promotes(self):
        result = review_canary(self.root, self.plan_id)
        self.assertEqual(result["status"], "BELOW_THRESHOLD")
        self.assertFalse(result["candidate_eligible"])
        self.assertEqual(result["alpha_id"], "TESTALPHA")
        self.assertEqual(result["failed_metrics"], ["sharpe", "fitness", "turnover"])
        self.assertEqual(result["metrics"]["sharpe"], 0.09)
        self.assertEqual(result["ra_check_state"], "unavailable")
        self.assertTrue(result["platform_evidence_verified"])
        self.assertFalse(result["correlation_checked"])
        self.assertFalse(result["simulation_invoked"])
        self.assertFalse(result["alpha_submission_invoked"])
        self.assertEqual(review_canary(self.root, self.plan_id)["status"], "BELOW_THRESHOLD")

    def test_cli_review_reachable_without_credentials_and_no_gateway(self):
        args = SimpleNamespace(command="canary-review", plan_id=self.plan_id)
        out = StringIO()
        with (patch.dict("os.environ", {"BRAIN_CREDENTIALS_FILE": ""}),
              redirect_stdout(out)):
            result = run_canary_command(
                args, self.root,
                gateway_factory=lambda: self.fail("Gateway must not be constructed"))
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(out.getvalue())["status"], "BELOW_THRESHOLD")

    def test_modified_output_rejected_not_replaced(self):
        original = review_canary(self.root, self.plan_id)
        rows = json.loads(self.output_path.read_text(encoding="utf-8"))
        rows[0]["is"]["sharpe"] = 4.0
        atomic_write_json(self.output_path, rows)
        with self.assertRaisesRegex(Exception, "hash mismatch"):
            review_canary(self.root, self.plan_id)
        saved = json.loads(Path(original["review_path"]).read_text(encoding="utf-8"))
        self.assertEqual(saved["metrics"]["sharpe"], 0.09)

    def test_platform_omits_test_period_but_post_evidence_is_still_verified(self):
        output = json.loads(self.output_path.read_text(encoding="utf-8"))
        del output[0]["details"]["settings"]["testPeriod"]
        atomic_write_json(self.output_path, output)
        progress = json.loads(self.progress_path.read_text(encoding="utf-8"))
        progress["output_sha256"] = file_sha256(self.output_path)
        atomic_write_json(self.progress_path, progress)
        review = review_canary(self.root, self.plan_id)
        settings = review["settings_readback_verification"]
        self.assertEqual(settings["status"], "PARTIAL_READBACK")
        self.assertEqual(settings["not_echoed"], ["testPeriod"])
        self.assertTrue(settings["submitted_settings_verified_by_receipt_and_fingerprint"])
        self.assertEqual(review["status"], "BELOW_THRESHOLD")
        self.assertEqual(review["failed_metrics"], ["sharpe", "fitness", "turnover"])

    def test_zero_length_test_period_alias_is_explicitly_recorded(self):
        output = json.loads(self.output_path.read_text(encoding="utf-8"))
        output[0]["details"]["settings"]["testPeriod"] = "P0Y"
        atomic_write_json(self.output_path, output)
        progress = json.loads(self.progress_path.read_text(encoding="utf-8"))
        progress["output_sha256"] = file_sha256(self.output_path)
        atomic_write_json(self.progress_path, progress)
        result = review_canary(self.root, self.plan_id)
        settings = result["settings_readback_verification"]
        self.assertEqual(settings["status"], "NORMALIZED_READBACK")
        self.assertEqual(settings["normalized"], ["testPeriod"])
        self.assertEqual(settings["not_echoed"], [])

    def test_absent_nonoptional_setting_does_not_pass(self):
        output = json.loads(self.output_path.read_text(encoding="utf-8"))
        del output[0]["details"]["settings"]["neutralization"]
        atomic_write_json(self.output_path, output)
        progress = json.loads(self.progress_path.read_text(encoding="utf-8"))
        progress["output_sha256"] = file_sha256(self.output_path)
        atomic_write_json(self.progress_path, progress)
        with self.assertRaisesRegex(Exception, "keys: neutralization"):
            review_canary(self.root, self.plan_id)

    def test_source_settings_mismatch_rejected(self):
        output = json.loads(self.output_path.read_text(encoding="utf-8"))
        output[0]["details"]["settings"]["delay"] = 0
        atomic_write_json(self.output_path, output)
        progress = json.loads(self.progress_path.read_text(encoding="utf-8"))
        progress["output_sha256"] = file_sha256(self.output_path)
        atomic_write_json(self.progress_path, progress)
        with self.assertRaisesRegex(Exception, "settings do not match"):
            review_canary(self.root, self.plan_id)

    def test_missing_ra_does_not_get_assumed_pass(self):
        result = review_canary(self.root, self.plan_id)
        self.assertNotEqual(result["ra_check_state"], "pass")
        self.assertFalse(result["candidate_eligible"])


if __name__ == "__main__":
    unittest.main()
