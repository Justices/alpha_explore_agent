"""Offline regression for redacted complete-catalog failure diagnostics."""
import unittest

from alpha_research.integrations.brain.catalog_error_codes import (
    CATALOG_FAILURE_CODES, classify_catalog_failure,
)
from alpha_research.integrations.brain.runtime import PlatformError
from alpha_research.integrations.brain.gateway import Gateway, GatewayError
from alpha_research.workflows.regular.scope import discover_regular_scope


class CatalogFailureCodeTests(unittest.TestCase):
    def test_platform_error_categories_never_echo_upstream_text(self):
        examples = {
            "platform catalog changed during pagination": "CATALOG_PAGE_COUNT_CHANGED",
            "platform catalog contains duplicate records": "CATALOG_DUPLICATE_IDS",
            "platform catalog is incomplete": "CATALOG_PAGE_INCOMPLETE",
            "platform request returned HTTP 400": "CATALOG_REQUEST_REJECTED",
            "platform request returned HTTP 401": "CATALOG_AUTHORIZATION_REJECTED",
            "platform request returned HTTP 429": "CATALOG_RATE_LIMITED",
            "platform request returned HTTP 503": "CATALOG_UPSTREAM_ERROR",
            "platform request failed: ReadTimeout": "CATALOG_NETWORK_ERROR",
            "private-secret-sentinel": "CATALOG_UNKNOWN",
        }
        for message, code in examples.items():
            with self.subTest(message=message):
                self.assertEqual(classify_catalog_failure(PlatformError(message)), code)
                self.assertIn(code, CATALOG_FAILURE_CODES)
        self.assertEqual(classify_catalog_failure(OSError("secret file")), "CATALOG_LOCAL_IO_ERROR")
        self.assertEqual(classify_catalog_failure(ValueError("secret value")), "CATALOG_UNKNOWN")

    def test_gateway_accepts_only_whitelisted_catalog_code(self):
        gateway = Gateway()
        gateway.proc = type("Process", (), {"poll": lambda self: None})()
        gateway._send = lambda _: {"ok": False, "error": "private-secret-sentinel",
                                   "error_code": "CATALOG_DUPLICATE_IDS"}
        with self.assertRaises(GatewayError) as caught:
            gateway.call("get_datasets", instrument_type="EQUITY",
                         region="USA", delay=1, universe="TOP3000")
        self.assertEqual(caught.exception.code, "CATALOG_DUPLICATE_IDS")
        self.assertNotIn("private-secret-sentinel", str(caught.exception))
        with self.assertRaises(GatewayError) as other:
            gateway.call("simulate_batch")
        self.assertIsNone(other.exception.code)
        gateway._send = lambda _: {"ok": False, "error": "private-secret-sentinel",
                                   "error_code": "private-secret-sentinel"}
        with self.assertRaises(GatewayError) as unknown:
            gateway.call("get_datasets")
        self.assertIsNone(unknown.exception.code)
        self.assertNotIn("private-secret-sentinel", str(unknown.exception))

    def test_scope_reports_code_and_stays_unresolved(self):
        class GatewayStub:
            def call(self, command, **kwargs):
                if command == "get_platform_setting_options":
                    return {"instrument_options": [{
                        "InstrumentType": "EQUITY", "Region": "USA",
                        "Delay": 1, "Universe": ["TOP3000"],
                    }]}
                if command == "get_datasets":
                    raise GatewayError("unavailable", code="CATALOG_PAGE_COUNT_CHANGED")
                raise AssertionError("no other request is allowed")
        result = discover_regular_scope(
            GatewayStub(), region="USA", delay=1,
            universe="TOP3000", dataset_ids=["analyst10"])
        self.assertFalse(result["coverage_complete"])
        self.assertEqual(result["datasets"], [])
        self.assertEqual(result["catalogs"], [])
        self.assertEqual(result["unresolved"][0]["catalog_error_code"],
                         "CATALOG_PAGE_COUNT_CHANGED")


if __name__ == "__main__":
    unittest.main()
