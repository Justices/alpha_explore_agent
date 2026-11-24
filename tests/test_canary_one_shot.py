"""Canary offline contract tests: no real BRAIN, model or Alpha submission."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from alpha_research.cli_canary import (
    EXPRESSION, FIELD, _effective_settings, run_canary_command,
)
from alpha_research.state.store import atomic_write_json


class FakeGateway:
    def __init__(self, fail_post=False):
        self.calls = []
        self.fail_post = fail_post

    def start(self):
        self.calls.append(("start", {}))

    def shutdown(self):
        self.calls.append(("shutdown", {}))

    def call(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command == "get_platform_setting_options":
            return {
                "instrument_options": [{
                    "InstrumentType": "EQUITY", "Region": "USA",
                    "Delay": 1, "Universe": ["TOP3000"],
                    "Neutralization": ["INDUSTRY"],
                }],
                "simulation_settings_schema": {
                    "decay": {"minValue": 0, "maxValue": 20},
                    "truncation": {"minValue": 0.0, "maxValue": 1.0},
                    "pasteurization": {"choices": [{"value": "ON"}]},
                    "unitHandling": {"choices": [{"value": "VERIFY"}]},
                    "nanHandling": {"choices": [{"value": "OFF"}]},
                    "maxTrade": {"choices": [{"value": "OFF"}]},
                    "testPeriod": {"choices": [{"value": "P0Y0M"}]},
                },
            }
        if command == "get_datafields":
            return [{"id": FIELD, "type": "MATRIX",
                     "description": "Analyst revision ratio",
                     "dataset": {"id": "analyst10"}}]
        if command == "get_operators":
            return [{"name": "rank", "scope": ["REGULAR"],
                     "definition": "rank(x)"}]
        if command == "simulate_canary_once":
            if self.fail_post:
                raise RuntimeError("secret upstream response")
            return {
                "results": [{"alpha_id": "ALPHA1", "expression": EXPRESSION,
                             "success": True, "is": {"sharpe": 1.2}}],
                "runner": {"status": "COMPLETE"},
            }
        if command in ("set_case", "validate_simulation_settings",
                       "check_simulation_cache"):
            return {"status": "clear"}
        raise AssertionError("unexpected Gateway command: " + command)


class CanaryTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)
        self.plan_id = "a" * 32
        plan = {
            "status": "planned", "plan_id": self.plan_id,
            "region": "USA", "coverage_complete": True, "unresolved": [],
            "requested": {"dataset_ids": ["analyst10"], "category": None,
                          "delay": 1, "universe": "TOP3000"},
            "datasets": [{"dataset_id": "analyst10", "category": "analyst",
                          "combinations": [{
                              "region": "USA", "delay": 1, "universe": "TOP3000",
                              "detail_scope": {"region": "USA", "delay": 1,
                                               "universe": "TOP3000"},
                              "scope_evidence_source": "exact_catalog",
                          }]}],
        }
        atomic_write_json(self.root / "state" / "research_v1_1" /
                          "plans" / (self.plan_id + ".json"), plan)

    def invoke(self, cmd, gateway, *, allow=False):
        args = SimpleNamespace(command=cmd, plan_id=self.plan_id,
                               allow_one_simulation=allow)
        stdout, stderr = io.StringIO(), io.StringIO()
        with (patch.dict("os.environ", {"BRAIN_CREDENTIALS_FILE": "fake-configured"}),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            rc = run_canary_command(args, self.root, gateway_factory=lambda: gateway)
        return rc, (json.loads(stdout.getvalue()) if stdout.getvalue() else None), stderr.getvalue()

    def test_local_prepare_does_not_post(self):
        g = FakeGateway()
        rc, result, error = self.invoke("canary-prepare", g)
        self.assertEqual(rc, 0, error)
        self.assertEqual(result["status"], "PREPARED_NO_POST")
        self.assertEqual(result["expression"], EXPRESSION)
        self.assertFalse(any(c.startswith("simulate") for c, _ in g.calls))
        self.assertEqual(self.invoke("canary-status", g)[1]["status"], "PREPARED")

    def test_explicit_exactly_once_post_and_readback(self):
        g = FakeGateway()
        self.assertEqual(self.invoke("canary-prepare", g)[0], 0)
        self.assertEqual(self.invoke("canary-execute", g, allow=False)[0], 2)
        self.assertEqual(self.invoke("canary-execute", g, allow=True)[0], 0)
        sends = [args for cmd, args in g.calls if cmd == "simulate_canary_once"]
        self.assertEqual(len(sends), 1)
        self.assertEqual(len(sends[0]["variants"]), 1)
        self.assertEqual(sends[0]["variants"][0]["expression"], EXPRESSION)
        self.assertFalse(any(cmd in {"tag_alpha", "submit_alpha"}
                             for cmd, _ in g.calls))
        self.assertEqual(self.invoke("canary-status", g)[1]["status"],
                         "COMPLETED_READBACK")
        self.assertEqual(self.invoke("canary-execute", g, allow=True)[0], 2)
        self.assertEqual(len([1 for cmd, _ in g.calls
                              if cmd == "simulate_canary_once"]), 1)

    def test_lost_response_consumes_attempt_without_retry(self):
        g = FakeGateway(fail_post=True)
        self.assertEqual(self.invoke("canary-prepare", g)[0], 0)
        rc, _, error = self.invoke("canary-execute", g, allow=True)
        self.assertEqual(rc, 2)
        self.assertIn("DO NOT RETRY", error)
        self.assertNotIn("secret upstream", error)
        self.assertEqual(self.invoke("canary-status", g)[1]["status"],
                         "ATTEMPT_RESERVED")
        self.assertEqual(self.invoke("canary-execute", g, allow=True)[0], 2)
        self.assertEqual(len([1 for cmd, _ in g.calls
                              if cmd == "simulate_canary_once"]), 1)

    def test_scope_plan_tampering_blocks_post(self):
        g = FakeGateway()
        self.assertEqual(self.invoke("canary-prepare", g)[0], 0)
        path = self.root / "state" / "research_v1_1" / "plans" / (self.plan_id + ".json")
        plan = json.loads(path.read_text(encoding="utf-8"))
        plan["extra"] = "tampered"
        atomic_write_json(path, plan)
        self.assertEqual(self.invoke("canary-execute", g, allow=True)[0], 2)
        self.assertFalse(any(cmd == "simulate_canary_once" for cmd, _ in g.calls))

    def test_unsupported_live_setting_fails_closed(self):
        g = FakeGateway()
        options = g.call("get_platform_setting_options")
        options["instrument_options"][0]["Neutralization"] = ["SECTOR"]
        with self.assertRaisesRegex(Exception, "INDUSTRY"):
            _effective_settings(options)
        self.assertFalse(any(cmd == "simulate_canary_once" for cmd, _ in g.calls))


if __name__ == "__main__":
    unittest.main()
