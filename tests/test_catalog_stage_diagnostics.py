"""Complete Dataset read failures are classified at a fixed redacted stage."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from alpha_research.integrations.brain.catalog_error_codes import (
    CATALOG_FAILURE_STAGES, catalog_stage, catalog_failure_stage,
    catalog_exception_kind, classify_catalog_failure,
)
from alpha_research.integrations.brain.runtime import PlatformError, PlatformRuntime
from alpha_research.integrations.brain.gateway import Gateway, GatewayError
from alpha_research.state.writer_lock import WriterLockError
from alpha_research.workflows.regular.scope import discover_regular_scope


class DummyClient:
    base_url = "https://api.worldquantbrain.com"
    catalog_account = "test-user"

    def request(self, method, path, *, params=None, json=None):
        raise PlatformError("platform request returned HTTP 400")

    def close(self):
        pass


class CatalogStageDiagnosticTests(unittest.TestCase):
    def test_inner_stage_wins_and_retains_exception(self):
        with self.assertRaises(PlatformError) as ctx:
            with catalog_stage("CATALOG_LOCK"):
                with catalog_stage("CATALOG_REMOTE_PAGINATION"):
                    raise PlatformError("platform catalog changed during pagination")
        self.assertEqual(catalog_failure_stage(ctx.exception), "CATALOG_REMOTE_PAGINATION")
        self.assertEqual(classify_catalog_failure(ctx.exception), "CATALOG_PAGE_COUNT_CHANGED")
        self.assertEqual(catalog_exception_kind(ctx.exception), "PLATFORM")
        self.assertNotIn("secret", str(ctx.exception))
        self.assertIn("CATALOG_LOCK", CATALOG_FAILURE_STAGES)

    def test_lock_failure_is_distinct_from_unexpected_exception(self):
        exc = WriterLockError("secret-sentinel")
        self.assertEqual(classify_catalog_failure(exc), "CATALOG_LOCK_UNAVAILABLE")
        self.assertEqual(catalog_exception_kind(exc), "LOCK")

    def test_live_runtime_failure_marks_pagination_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = PlatformRuntime(root / "state", client=DummyClient())
            runtime._snapshot_root = root / "knowledge"
            runtime.set_case("case", "run")
            with self.assertRaises(PlatformError) as ctx:
                runtime.dispatch("get_datasets", {
                    "instrument_type": "EQUITY", "region": "USA",
                    "delay": 1, "universe": "TOP3000"})
            self.assertEqual(catalog_failure_stage(ctx.exception), "CATALOG_REMOTE_PAGINATION")
            self.assertEqual(classify_catalog_failure(ctx.exception), "CATALOG_REQUEST_REJECTED")
            self.assertIsNone((root / "knowledge" / "datasets" / "USA" / "1" /
                               "TOP3000" / "datasets.json").exists() or None)

    def test_full_read_unexpected_platform_error_carries_stage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = PlatformRuntime(root / "state", client=DummyClient())
            runtime._snapshot_root = root / "knowledge"
            runtime.set_case("case", "run")
            with patch.object(runtime, "_cached_catalog",
                              side_effect=TypeError("secret-sentinel")):
                with self.assertRaises(TypeError) as ctx:
                    runtime.dispatch("get_datasets", {
                        "region": "USA", "delay": 1, "universe": "TOP3000"})
            self.assertEqual(catalog_failure_stage(ctx.exception), "CATALOG_SNAPSHOT_READ")
            self.assertEqual(catalog_exception_kind(ctx.exception), "DATA_SHAPE")
            self.assertEqual(classify_catalog_failure(ctx.exception), "CATALOG_UNKNOWN")

    def test_scope_propagates_only_allowlisted_fields(self):
        class GatewayStub:
            def call(self, cmd, **kwargs):
                if cmd == "get_platform_setting_options":
                    return {"instrument_options": [{
                        "InstrumentType": "EQUITY", "Region": "USA",
                        "Delay": 1, "Universe": ["TOP3000"]}]}
                if cmd == "get_datasets":
                    raise GatewayError("secret-sentinel", code="CATALOG_UNKNOWN",
                                       stage="CATALOG_SNAPSHOT_READ", kind="DATA_SHAPE")
                raise AssertionError("unexpected command")
        result = discover_regular_scope(GatewayStub(), region="USA", delay=1,
                                        universe="TOP3000", dataset_ids=["analyst10"])
        issue = result["unresolved"][0]
        self.assertEqual(issue["catalog_error_code"], "CATALOG_UNKNOWN")
        self.assertEqual(issue["catalog_error_stage"], "CATALOG_SNAPSHOT_READ")
        self.assertEqual(issue["catalog_error_kind"], "DATA_SHAPE")
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(result["datasets"], [])
        self.assertNotIn("secret-sentinel", str(result))


if __name__ == "__main__":
    unittest.main()
