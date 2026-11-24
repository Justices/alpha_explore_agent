"""Offline proof that driver-level Catalog failures expose only safe metadata."""
import io
import json
import sys
import unittest
from unittest.mock import patch

from alpha_research.integrations.brain import driver
from alpha_research.integrations.brain.catalog_error_codes import (
    safe_catalog_error_source,
)
from alpha_research.integrations.brain.gateway import Gateway, GatewayError
from alpha_research.workflows.regular.scope import discover_regular_scope


class FakeRuntime:
    def __init__(self, fail_dispatch=False):
        self.fail_dispatch = fail_dispatch
        self.closed = False

    def dispatch(self, cmd, params):
        if cmd == "get_datasets":
            if self.fail_dispatch:
                raise TypeError("private-sentinel")
            return [{"id": "analyst10"}]
        raise AssertionError("unexpected command")

    def close(self):
        self.closed = True


class DriverBoundaryDiagnostics(unittest.TestCase):
    def run_driver(self, *, fail_dispatch=False, fail_write=False):
        runtime = FakeRuntime(fail_dispatch=fail_dispatch)
        events = []

        def safe_write(payload):
            if fail_write and payload.get("ok") and "result" in payload:
                raise TypeError("private-sentinel")
            events.append(payload)

        requests = io.StringIO('{"cmd":"get_datasets"}\\n{"cmd":"shutdown"}\\n'.replace("\\n", "\n"))
        with (patch.object(sys, "argv", ["driver"]),
              patch.object(sys, "stdin", requests),
              patch.object(driver, "PlatformRuntime", return_value=runtime),
              patch.object(driver, "write", side_effect=safe_write)):
            result = driver.main()
        self.assertEqual(result, 0)
        self.assertTrue(runtime.closed)
        self.assertNotIn("private-sentinel", json.dumps(events))
        return events

    def test_dispatch_exception_is_classified_outside_runtime_stage(self):
        events = self.run_driver(fail_dispatch=True)
        failure = events[1]
        self.assertEqual(failure["error_code"], "CATALOG_UNKNOWN")
        self.assertEqual(failure["error_kind"], "DATA_SHAPE")
        self.assertEqual(failure["error_phase"], "PLATFORM_DISPATCH")
        self.assertNotIn("error_stage", failure)
        self.assertEqual(failure["error_source"]["module"],
                         "alpha_research.integrations.brain.driver")
        self.assertEqual(failure["error_source"]["function"], "main")

    def test_response_serialization_failure_is_classified(self):
        events = self.run_driver(fail_write=True)
        failure = events[1]
        self.assertEqual(failure["error_phase"], "FRAME_WRITE")
        self.assertEqual(failure["error_kind"], "DATA_SHAPE")
        self.assertNotIn("error_stage", failure)

    def test_driver_jsonl_is_unicode_safe_on_strict_utf8_pipe(self):
        # A remote catalog description can contain lone surrogate escapes.
        # Legacy ensure_ascii=False raised UnicodeEncodeError in child stdout,
        # outside catalog Runtime stages; the JSON wire must stay ASCII.
        destination = io.BytesIO()
        pipe = io.TextIOWrapper(destination, encoding="utf-8",
                                errors="strict", write_through=True)
        with patch.object(driver.sys, "stdout", pipe):
            driver.write({"ok": True, "result": [
                {"id": "analyst10", "description": "中文" + "\\ud800"}]})
        payload = destination.getvalue()
        self.assertTrue(payload.isascii())
        parsed = json.loads(payload)
        self.assertEqual(parsed["result"][0]["description"], "中文" + "\\ud800")

    def test_gateway_rejects_untrusted_error_source_values(self):
        gateway = Gateway()
        gateway.proc = type("P", (), {"poll": lambda self: None})()
        gateway._send = lambda _: {
            "ok": False, "error_code": "CATALOG_UNKNOWN",
            "error_phase": "FRAME_WRITE", "error_kind": "DATA_SHAPE",
            "error_source": {"module": "private-secret", "function": "secret", "line": 9},
        }
        with self.assertRaises(GatewayError) as caught:
            gateway.call("get_datasets")
        self.assertEqual(caught.exception.phase, "FRAME_WRITE")
        self.assertIsNone(caught.exception.source)

    def test_scope_preserves_only_valid_phase(self):
        class Stub:
            def call(self, cmd, **kwargs):
                if cmd == "get_platform_setting_options":
                    return {"instrument_options": [{
                        "InstrumentType": "EQUITY", "Region": "USA",
                        "Delay": 1, "Universe": ["TOP3000"]}]}
                if cmd == "get_datasets":
                    raise GatewayError("secret", code="CATALOG_UNKNOWN",
                                       phase="PLATFORM_DISPATCH", kind="DATA_SHAPE")
                raise AssertionError(cmd)
        plan = discover_regular_scope(Stub(), region="USA", delay=1,
                                      universe="TOP3000", dataset_ids=["analyst10"])
        issue = plan["unresolved"][0]
        self.assertEqual(issue["catalog_error_phase"], "PLATFORM_DISPATCH")
        self.assertNotIn("catalog_error_source", issue)
        self.assertFalse(plan["coverage_complete"])


if __name__ == "__main__":
    unittest.main()
