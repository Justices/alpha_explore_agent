"""Official read-only recordset capture never becomes simulated CSV evidence."""
import json
import os
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from alpha_research.cli_canary import EXPRESSION, run_canary_command
from alpha_research.cli_canary_recordsets import (
    _get, observe_canary_recordsets,
)
from alpha_research.cli_parser import build_parser
from alpha_research.state.store import atomic_write_json
from tests.test_canary_distribution import CanaryDistributionTests


class FakeResponse:
    def __init__(self, status, data=None):
        self.status_code = status
        self._data = data
        self.content = json.dumps(data).encode("utf-8") if data is not None else b""

    def json(self):
        return self._data


class FakeGetOnlyClient:
    def __init__(self, alpha_id="TESTALPHA", *, expression=EXPRESSION,
                 settings=None, recordsets=None):
        self.alpha_id = alpha_id
        self.paths = []
        self.settings = settings if settings is not None else {
            "instrumentType": "EQUITY", "region": "USA",
            "delay": 1, "universe": "TOP3000",
        }
        self.expression = expression
        self.recordsets = recordsets if recordsets is not None else {
            "coverage": {
                "schema": {"name": "coverage", "properties": [
                    {"name": "date"}, {"name": "instruments"}]},
                "records": [["2026-11-20", 100], ["2026-11-23", 101]],
            },
            "turnover": {
                "schema": {"name": "turnover"},
                "records": [["2026-11-20", 0.31]],
            },
        }

    def request(self, method, path):
        if method != "GET":
            raise AssertionError("platform POST/PATCH/DELETE cannot be invoked")
        expected = "/alphas/" + self.alpha_id
        self.paths.append(path)
        if path == expected:
            return FakeResponse(200, {
                "id": self.alpha_id, "type": "REGULAR",
                "regular": {"code": self.expression},
                "settings": self.settings,
            })
        if path == expected + "/recordsets":
            return FakeResponse(200, {
                "count": len(self.recordsets),
                "results": [{"name": name, "title": name.title()}
                            for name in self.recordsets],
            })
        prefix = expected + "/recordsets/"
        if path.startswith(prefix):
            return FakeResponse(200, self.recordsets[path[len(prefix):]])
        return FakeResponse(404)

    def close(self):
        raise AssertionError("injected test client is not owned by observer")


class CanaryRecordsetsTests(unittest.TestCase):
    def setUp(self):
        f = CanaryDistributionTests(
            "test_no_data_has_explicit_missing_requirements_and_zero_metrics")
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.f = f
        self.root = f.root
        self.plan_id = f.plan_id

    def _observe(self, client):
        return observe_canary_recordsets(
            self.root, self.plan_id, client=client,
            snapshot_root=self.f.snapshots)

    def test_get_only_capture_pins_alpha_and_retains_unaltered_schema(self):
        client = FakeGetOnlyClient()
        result = self._observe(client)
        self.assertEqual(result["status"],
                         "ALPHA_RECORDSETS_CAPTURED_SCHEMA_REVIEW_REQUIRED")
        self.assertEqual(result["source_alpha_id"], "TESTALPHA")
        self.assertEqual(result["recordsets"]["coverage"]["record_count"], 2)
        self.assertEqual(result["recordsets"]["coverage-by-sector"]["status"],
                         "NOT_LISTED")
        self.assertTrue(result["alpha_level_recordset_not_raw_field_or_position_evidence"])
        self.assertFalse(result["simulation_authorized"])
        self.assertFalse(result["candidate_eligible"])
        path = Path(result["recordset_evidence_path"])
        evidence = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(evidence["payloads"]["coverage"]["records"][0][1], 100)
        self.assertEqual(len(client.paths), 4)
        self.assertEqual(self._observe(FakeGetOnlyClient())["recordset_evidence_path"],
                         str(path))

    def test_wrong_expression_or_scope_rejected_before_recordset_reads(self):
        wrong = FakeGetOnlyClient(expression="rank(close)")
        with self.assertRaisesRegex(Exception, "expression differs"):
            self._observe(wrong)
        self.assertEqual(wrong.paths, ["/alphas/TESTALPHA"])
        wrong_scope = FakeGetOnlyClient(settings={
            "instrumentType": "EQUITY", "region": "CHN",
            "delay": 1, "universe": "TOP3000",
        })
        with self.assertRaisesRegex(Exception, "scope differs"):
            self._observe(wrong_scope)
        self.assertEqual(wrong_scope.paths, ["/alphas/TESTALPHA"])
        bool_delay = FakeGetOnlyClient(settings={
            "instrumentType": "EQUITY", "region": "USA",
            "delay": True, "universe": "TOP3000",
        })
        with self.assertRaisesRegex(Exception, "scope differs"):
            self._observe(bool_delay)

    def test_recordset_http_unavailability_is_not_fabricated(self):
        class NoIndex(FakeGetOnlyClient):
            def request(self, method, path):
                if path.endswith("/recordsets"):
                    self.paths.append(path)
                    return FakeResponse(403)
                return super().request(method, path)
        result = self._observe(NoIndex())
        self.assertEqual(result["status"], "ALPHA_RECORDSETS_UNAVAILABLE")
        self.assertEqual(result["recordset_catalog"]["http_status"], 403)
        self.assertFalse(result["source_alpha_id"] == "")

    def test_invalid_read_path_blocked_without_request(self):
        client = FakeGetOnlyClient()
        with self.assertRaisesRegex(Exception, "allowlist"):
            _get(client, "/simulations")
        with self.assertRaisesRegex(Exception, "allowlist"):
            _get(client, "/alphas/TESTALPHA/submit")
        self.assertEqual(client.paths, [])

    def test_manifest_or_prior_evidence_unchanged_and_report_is_immutable(self):
        result = self._observe(FakeGetOnlyClient())
        evidence_path = Path(result["recordset_evidence_path"])
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        evidence["report"]["simulation_authorized"] = True
        atomic_write_json(evidence_path, evidence)
        with self.assertRaisesRegex(Exception, "differing"):
            self._observe(FakeGetOnlyClient())
        self.assertFalse((self.f.inputs / "evidence_manifest.json").exists())

    def test_read_permission_is_explicit_cli_default_deny(self):
        args = build_parser().parse_args(
            ["canary-recordsets-observe", "--plan-id", self.plan_id])
        self.assertFalse(args.allow_brain_read)
        stderr = StringIO()
        with (patch.dict(os.environ, {"BRAIN_CREDENTIALS_FILE": ""}),
              redirect_stderr(stderr)):
            rc = run_canary_command(
                args, self.root,
                gateway_factory=lambda: self.fail("Gateway must not launch"))
        self.assertEqual(rc, 2)
        self.assertIn("--allow-brain-read", stderr.getvalue())
        self.assertFalse(list((self.root / "state" / "canary").glob(
            "*_recordsets_*.json")))


if __name__ == "__main__":
    unittest.main()
