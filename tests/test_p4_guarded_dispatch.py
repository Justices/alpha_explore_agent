"""P4.5 guarded production boundary: no real BRAIN or paid model calls."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from alpha_research.integrations.brain import driver
from alpha_research.integrations.brain.gateway import Gateway, GatewayError
from alpha_research.orchestration.engine import MainOrchestrator
from alpha_research.orchestration.simulation_dispatch_preflight import (
    inspect_simulation_dispatch_gate,
)


class GuardedGatewayTests(unittest.TestCase):
    def test_simulation_mutations_refused_before_driver(self):
        gateway = Gateway(orchestration_guarded=True)
        for command in (
            "simulate_batch", "simulate_super", "tag_alpha",
            "sync_backtest_cache", "resume_simulation_batch",
            "set_case", "future_mutation_not_in_whitelist",
        ):
            with self.subTest(command=command):
                with self.assertRaisesRegex(
                        GatewayError, "trusted live admission"):
                    gateway.call(command, allow=True, authorization_id="a" * 32)
        self.assertIsNone(gateway.proc)

    def test_guarded_driver_prevents_raw_simulation_injection(self):
        class FakeRuntime:
            def dispatch(self, command, params):
                raise AssertionError("forbidden command reached platform")
            def close(self):
                pass
        fake_stdin = io.StringIO(
            '{"cmd":"simulate_super","allow":true,"authorization_id":"foo"}\n'
            '{"cmd":"future_mutation_not_in_whitelist","allow":true}\n'
            '{"cmd":"shutdown"}\n')
        out = io.StringIO()
        with (patch.object(driver.sys, "argv",
                           ["driver.py", "--orchestration-guarded"]),
              patch.object(driver.sys, "stdin", fake_stdin),
              patch.object(driver.sys, "stdout", out),
              patch.object(driver, "PlatformRuntime", return_value=FakeRuntime())):
            rc = driver.main()
        self.assertEqual(rc, 0)
        responses = [json.loads(row) for row in out.getvalue().splitlines()]
        self.assertFalse(responses[1]["ok"])
        self.assertIn("admission", responses[1]["error"])
        self.assertFalse(responses[2]["ok"])

    def test_guarded_driver_redacts_provider_exception(self):
        class FakeRuntime:
            def dispatch(self, command, params):
                raise RuntimeError("sensitive-account-secret")
            def close(self):
                pass
        input_stream = io.StringIO(
            '{"cmd":"get_platform_setting_options"}\n'
            '{"cmd":"shutdown"}\n')
        output = io.StringIO()
        with (patch.object(driver.sys, "argv",
                           ["driver.py", "--orchestration-guarded"]),
              patch.object(driver.sys, "stdin", input_stream),
              patch.object(driver.sys, "stdout", output),
              patch.object(driver, "PlatformRuntime", return_value=FakeRuntime())):
            result = driver.main()
        self.assertEqual(result, 0)
        self.assertNotIn("sensitive-account-secret", output.getvalue())
        lines = [json.loads(x) for x in output.getvalue().splitlines()]
        self.assertFalse(lines[1]["ok"])

    def test_gateway_spawns_guarded_driver_flag(self):
        gateway = Gateway(orchestration_guarded=True)
        class Pipe:
            def write(self, *args):
                pass
            def flush(self):
                pass
        class Process:
            stdin = Pipe()
        def fake_popen(command, **kwargs):
            self.assertIn("--orchestration-guarded", command)
            return Process()
        with patch("alpha_research.integrations.brain.gateway.subprocess.Popen",
                   side_effect=fake_popen):
            gateway._read_response = lambda: {"ok": True, "stage": "ready"}
            gateway.start()


class GuardedDispatchAuditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.orchestrator = MainOrchestrator(self.root)
        self.task = self.orchestrator.create(
            "super", {"objective": "offline no-live guard",
                      "region": "USA", "delay": 1, "universe": "TOP3000"})
        self.policy = {"max_calls": 1, "max_reported_tokens": 1000,
                       "failover": "manual"}

    def test_audit_never_grants_execution(self):
        status = inspect_simulation_dispatch_gate(
            self.orchestrator, self.task["task_id"], self.policy)
        self.assertEqual(status["status"], "NOT_AUTHORIZED")
        self.assertFalse(status["permission_to_dispatch"])
        self.assertFalse(status["brain_simulation_authorized"])
        self.assertFalse(status["automatic_retry_allowed"])
        self.assertIn("TRUSTED_LIVE_GRANT_CONSUMER_NOT_IMPLEMENTED",
                      status["blockers"])

    def test_stop_flag_blocks_dispatch_without_network(self):
        stop = self.root / "state" / "stop.flag"
        stop.parent.mkdir(parents=True, exist_ok=True)
        stop.write_text("stop", encoding="utf-8")
        status = inspect_simulation_dispatch_gate(
            self.orchestrator, self.task["task_id"], self.policy)
        self.assertIn("GLOBAL_STOP_SIGNAL_PRESENT", status["blockers"])


if __name__ == "__main__":
    unittest.main()
