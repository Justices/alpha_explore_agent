"""P4.4 isolated read-only BRAIN contract: NO external connection."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from alpha_research import cli
from alpha_research.integrations.brain.gateway import Gateway, GatewayError
from alpha_research.orchestration.readonly_probe import run_readonly_probe


def platform_options():
    return {"instrument_options": [
        {"InstrumentType": "EQUITY", "Region": "CHN", "Delay": 1,
         "Universe": ["TOP3000"]},
    ]}


class FakeReadonlyGateway:
    read_only = True

    def __init__(self):
        self.calls = []

    def call(self, command, **params):
        self.calls.append((command, params))
        if command == "get_platform_setting_options":
            return platform_options()
        if command == "readonly_probe_datasets":
            return {"dataset_count": 7, "catalog_readable": True}
        raise AssertionError("mutating or unexpected Gateway command")


class ReadonlyBrainProbeTests(unittest.TestCase):
    def test_scope_and_dataset_probe(self):
        gateway = FakeReadonlyGateway()
        result = run_readonly_probe(
            gateway, region="CHN", delay=1, universe="TOP3000")
        self.assertEqual(result["status"], "READ_ONLY_SCOPE_VERIFIED")
        self.assertEqual(result["dataset_count"], 7)
        self.assertEqual([x[0] for x in gateway.calls],
                         ["get_platform_setting_options", "readonly_probe_datasets"])
        self.assertFalse(result["simulation_invoked"])
        self.assertFalse(result["model_invoked"])

    def test_unavailable_scope_does_not_read_catalog(self):
        gateway = FakeReadonlyGateway()
        result = run_readonly_probe(
            gateway, region="USA", delay=1, universe="TOP3000")
        self.assertEqual(result["status"], "SCOPE_NOT_AVAILABLE")
        self.assertEqual(len(gateway.calls), 1)

    def test_invalid_scope_fails_before_gateway_call(self):
        gateway = FakeReadonlyGateway()
        for region, delay, universe in (
            ("../CHN", 1, "TOP3000"),
            ("CHN", True, "TOP3000"),
            ("CHN", 1, "../../evil"),
        ):
            with self.subTest(region=region, delay=delay, universe=universe):
                with self.assertRaises(ValueError):
                    run_readonly_probe(gateway, region=region,
                                       delay=delay, universe=universe)
        self.assertEqual(gateway.calls, [])

    def test_gateway_refuses_all_mutations_before_driver(self):
        gateway = Gateway(read_only=True)
        for command in ("simulate_batch", "simulate_super", "set_case",
                        "tag_alpha", "resume_simulation_batch",
                        "sync_backtest_cache", "get_datasets"):
            with self.subTest(command=command):
                with self.assertRaisesRegex(GatewayError, "read-only"):
                    gateway.call(command)
        self.assertIsNone(gateway.proc)

    def test_cli_requires_explicit_opt_in_before_constructing_gateway(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(cli, "Gateway",
                              side_effect=AssertionError("Gateway forbidden")):
                with redirect_stderr(io.StringIO()):
                    rc = cli.main(["--state-root", td, "brain-readonly-check",
                                   "--region", "CHN", "--delay", "1",
                                   "--universe", "TOP3000"])
        self.assertEqual(rc, 2)

    def test_cli_explicit_read_has_no_model_and_sanitized_summary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            creds = root / "credentials.json"
            creds.write_text('{"email":"mock@example.invalid","password":"secret-sentinel"}',
                             encoding="utf-8")
            class Stub:
                read_only = True
                called = []
                def start(self):
                    self.called.append("start")
                    return {"ok": True}
                def call(self, cmd, **kwargs):
                    self.called.append(cmd)
                    return (platform_options() if cmd == "get_platform_setting_options"
                            else {"dataset_count": 7, "catalog_readable": True})
                def shutdown(self):
                    self.called.append("shutdown")
            gateway = Stub()
            stdout = io.StringIO()
            with (patch.dict(os.environ, {"BRAIN_CREDENTIALS_FILE": str(creds)}),
                  patch.object(cli, "Gateway", return_value=gateway) as ctor,
                  patch.object(cli, "build_model_client",
                               side_effect=AssertionError("model forbidden")),
                  redirect_stdout(stdout), redirect_stderr(io.StringIO())):
                rc = cli.main(["--state-root", str(root), "brain-readonly-check",
                               "--region", "CHN", "--delay", "1",
                               "--universe", "TOP3000", "--allow-brain-read"])
            self.assertEqual(rc, 0)
            ctor.assert_called_once_with(read_only=True)
            self.assertEqual(gateway.called,
                             ["start", "get_platform_setting_options",
                              "readonly_probe_datasets", "shutdown"])
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["status"], "READ_ONLY_SCOPE_VERIFIED")
            self.assertNotIn("secret-sentinel", stdout.getvalue())
            self.assertNotIn("mock@example.invalid", stdout.getvalue())
            self.assertFalse(result["alpha_submission_invoked"])

    def test_driver_read_only_rejects_raw_simulation_json(self):
        from alpha_research.integrations.brain import driver
        stdin = io.StringIO(
            '{"cmd":"simulate_batch","variants":[{"expression":"rank(x)"}]}\n'
            '{"cmd":"shutdown"}\n')
        stdout = io.StringIO()
        fake = type("FakeRuntime", (), {
            "dispatch": lambda self, *args: (_ for _ in ()).throw(
                AssertionError("driver may not dispatch mutator")),
            "close": lambda self: None})()
        with (patch.object(driver.sys, "argv", ["driver.py", "--read-only"]),
              patch.object(driver.sys, "stdin", stdin),
              patch.object(driver.sys, "stdout", stdout),
              patch.object(driver, "PlatformRuntime", return_value=fake)):
            code = driver.main()
        self.assertEqual(code, 0)
        responses = [json.loads(x) for x in stdout.getvalue().splitlines()]
        self.assertFalse(responses[1]["ok"])
        self.assertIn("read-only", responses[1]["error"])


if __name__ == "__main__":
    unittest.main()
