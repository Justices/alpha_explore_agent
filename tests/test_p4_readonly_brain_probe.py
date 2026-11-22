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
from alpha_research.orchestration.platform_read_evidence import (
    record_platform_read_evidence, inspect_platform_read_evidence,
)


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

    def test_verified_platform_read_evidence_is_local_non_authorizing(self):
        with tempfile.TemporaryDirectory() as td:
            probe = run_readonly_probe(
                FakeReadonlyGateway(), region="CHN", delay=1, universe="TOP3000")
            recorded = record_platform_read_evidence(td, probe)
            self.assertEqual(recorded["status"],
                             "LOCAL_READ_OBSERVED_NOT_ATTESTED")
            self.assertEqual(recorded["dataset_count"], 7)
            self.assertTrue(recorded["local_integrity_verified"])
            self.assertFalse(recorded["platform_account_verified"])
            self.assertFalse(recorded["production_authorized"])
            self.assertFalse(recorded["permission_to_dispatch"])
            self.assertFalse(recorded["model_call_authorized"])
            self.assertFalse(recorded["alpha_submission_authorized"])
            record_id = recorded["record_id"]
            persisted = (Path(td) / "state" / "research_v1_1"
                         / "platform_read_evidence" / (record_id + ".json"))
            data = json.loads(persisted.read_text())
            self.assertEqual(data["report"], probe)
            self.assertIn("state_sha256", data)
            self.assertNotIn("password", persisted.read_text())
            self.assertEqual(
                inspect_platform_read_evidence(td, record_id), recorded)

            data["report"]["dataset_count"] = 999
            persisted.write_text(json.dumps(data))
            tampered = inspect_platform_read_evidence(td, record_id)
            self.assertEqual(tampered["status"], "RECONCILIATION_REQUIRED")
            self.assertFalse(tampered["permission_to_dispatch"])

    def test_recorded_probe_rejects_pretend_success_or_model_invocation(self):
        with tempfile.TemporaryDirectory() as td:
            valid = run_readonly_probe(
                FakeReadonlyGateway(), region="CHN", delay=1, universe="TOP3000")
            for replacement in (
                {"status": "SCOPE_NOT_AVAILABLE"},
                {"simulation_invoked": True},
                {"model_invoked": True},
                {"alpha_submission_invoked": True},
                {"dataset_count": -1},
                {"extra_claim": "live"},
            ):
                with self.subTest(replacement=replacement):
                    with self.assertRaises(ValueError):
                        record_platform_read_evidence(
                            td, dict(valid, **replacement))
            self.assertFalse(
                (Path(td) / "state" / "research_v1_1"
                 / "platform_read_evidence").exists())

    def test_cli_record_read_evidence_and_offline_inspection(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            credentials = root / "credentials.json"
            credentials.write_text('{"username":"fake","password":"never-output"}')

            class Stub:
                read_only = True
                def start(self):
                    return {"ok": True}
                def call(self, command, **params):
                    if command == "get_platform_setting_options":
                        return platform_options()
                    if command == "readonly_probe_datasets":
                        return {"dataset_count": 7, "catalog_readable": True}
                    raise AssertionError("unexpected BRAIN command")
                def shutdown(self):
                    return None

            out = io.StringIO()
            with (patch.dict(os.environ, {"BRAIN_CREDENTIALS_FILE": str(credentials)}),
                  patch.object(cli, "Gateway", return_value=Stub()) as gateway_ctor,
                  patch.object(cli, "build_model_client",
                               side_effect=AssertionError("model forbidden")),
                  redirect_stdout(out), redirect_stderr(io.StringIO())):
                code = cli.main([
                    "--state-root", td, "brain-readonly-check",
                    "--region", "CHN", "--delay", "1",
                    "--universe", "TOP3000", "--allow-brain-read",
                    "--record-evidence",
                ])
            self.assertEqual(code, 0)
            gateway_ctor.assert_called_once_with(read_only=True)
            result = json.loads(out.getvalue())
            self.assertEqual(result["status"], "READ_ONLY_SCOPE_VERIFIED")
            self.assertEqual(result["evidence_status"],
                             "LOCAL_READ_OBSERVED_NOT_ATTESTED")
            self.assertNotIn("never-output", out.getvalue())
            inspected = io.StringIO()
            with (patch.object(cli, "Gateway",
                               side_effect=AssertionError("inspection may not connect")),
                  redirect_stdout(inspected), redirect_stderr(io.StringIO())):
                code = cli.main([
                    "--state-root", td,
                    "brain-read-evidence-status", result["evidence_record_id"]])
            self.assertEqual(code, 0)
            self.assertEqual(
                json.loads(inspected.getvalue())["dataset_count"], 7)

    def test_stop_and_mock_marker_block_local_read_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            probe = run_readonly_probe(
                FakeReadonlyGateway(), region="CHN", delay=1, universe="TOP3000")
            stop = Path(td) / "state" / "stop.flag"
            stop.parent.mkdir(parents=True)
            stop.write_text("stop")
            with self.assertRaisesRegex(ValueError, "while stopped"):
                record_platform_read_evidence(td, probe)
            stop.unlink()
            from alpha_research.orchestration.mock_workspace import marker_path
            marker = marker_path(td)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("mock")
            with self.assertRaisesRegex(ValueError, "mock workspace"):
                record_platform_read_evidence(td, probe)

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
