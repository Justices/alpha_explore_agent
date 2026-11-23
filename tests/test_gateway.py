import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alpha_research.integrations.brain.gateway import Gateway, GatewayError


class TestGateway(unittest.TestCase):
    def test_read_only_session_failure_is_returned_without_gateway_retry(self):
        gateway = Gateway()
        gateway.proc = type("Process", (), {"poll": lambda self: None})()
        sent = []
        gateway._send = lambda payload: sent.append(payload) or {
            "ok": False, "error": "401 Unauthorized"}
        with self.assertRaisesRegex(GatewayError, "failed in platform driver") as caught:
            gateway.call("get_datasets", region="GLB")
        self.assertNotIn("401 Unauthorized", str(caught.exception))
        self.assertEqual([item["cmd"] for item in sent], ["get_datasets"])

    def test_expired_session_never_reposts_simulation(self):
        gateway = Gateway()
        gateway.proc = type("Process", (), {"poll": lambda self: None})()
        sent = []
        gateway._send = lambda payload: sent.append(payload) or {
            "ok": False, "error": "401 Unauthorized"}
        with self.assertRaisesRegex(GatewayError, "failed in platform driver") as caught:
            gateway.call("simulate_batch", variants=[{"expression": "rank(x)"}])
        self.assertNotIn("401 Unauthorized", str(caught.exception))
        self.assertEqual([item["cmd"] for item in sent], ["simulate_batch"])

    def test_gateway_auth_failure_never_exposes_driver_response(self):
        gateway = Gateway()
        class FakePipe:
            def write(self, *args):
                pass
            def flush(self):
                pass
            def close(self):
                pass
        class FakeProcess:
            stdin = FakePipe()
            stdout = FakePipe()
            def poll(self):
                return None
            def terminate(self):
                pass
            def wait(self, timeout=None):
                return 0
            def kill(self):
                pass
        from unittest.mock import patch
        with patch("alpha_research.integrations.brain.gateway.subprocess.Popen",
                   return_value=FakeProcess()):
            gateway._read_response = lambda: {
                "ok": False, "error": "credential: provider-secret-sentinel"}
            with self.assertRaisesRegex(GatewayError, "platform runtime unavailable") as caught:
                gateway.start()
        self.assertNotIn("provider-secret-sentinel", str(caught.exception))

    def test_direct_simulation_creation_is_not_exposed(self):
        gateway = Gateway()
        with self.assertRaisesRegex(GatewayError, "not in whitelist"):
            gateway.call("create_simulation", expression="rank(x)")

    def test_correlation_fails_closed_without_alpha_state(self):
        gateway = Gateway()
        with self.assertRaises(GatewayError):
            gateway.call("check_correlation", alpha_id="A1")

    def test_correlation_rejects_state_label_without_ra_evidence(self):
        gateway = Gateway(alpha_lookup=lambda alpha_id: {"state": "RA_PASSED"})
        with self.assertRaises(Exception) as caught:
            gateway.call("check_correlation", alpha_id="A1")
        self.assertIn("RA", str(caught.exception))

    def test_platform_correlation_requires_local_self_prefilter(self):
        alpha = {"state": "RA_PASSED", "ra_fail_count": 0, "ra_readable": True,
                 "self_corr_max": 0.7, "local_self": {"status": "pending"}}
        gateway = Gateway(alpha_lookup=lambda alpha_id: alpha)
        gateway.proc = type("Process", (), {"poll": lambda self: None})()
        sent = []
        gateway._send = lambda payload: sent.append(payload) or {"ok": True, "result": {}}
        with self.assertRaises(GatewayError):
            gateway.call("check_correlation", alpha_id="A1")
        self.assertEqual(sent, [])
        alpha["local_self"] = {"status": "pass", "value": 0.4, "complete": True,
                               "comparison_count": 1, "window_years": 4, "limit": 0.7}
        gateway.call("check_correlation", alpha_id="A1")
        self.assertEqual([row["cmd"] for row in sent], ["check_correlation"])

    def test_composite_submission_check_is_not_exposed(self):
        gateway = Gateway()
        with self.assertRaises(GatewayError):
            gateway.call("get_submission_check", alpha_id="A1")

    def test_service_can_bind_fresh_audit_evidence_without_regular_state_file(self):
        gateway = Gateway()
        gateway.proc = type("Process", (), {"poll": lambda self: None})()
        calls = []
        gateway._send = lambda payload: calls.append(payload) or {"ok": True, "result": {}}
        details = {"id": "A1", "is": {"checks": [{"name": "LOW_SHARPE", "result": "PASS"}]}}
        gateway.set_audit_evidence(details, 0.70)
        gateway.call("check_local_self_correlation", alpha_id="A1")
        with self.assertRaises(GatewayError):
            gateway.call("check_correlation", alpha_id="A1")
        gateway.set_audit_evidence(details, 0.70, {"status": "pass", "complete": True,
            "value": 0.4, "comparison_count": 1, "window_years": 4, "limit": 0.70})
        gateway.call("check_correlation", alpha_id="A1")
        gateway.clear_audit_evidence("A1")
        with self.assertRaises(GatewayError):
            gateway.call("check_local_self_correlation", alpha_id="A1")
        self.assertEqual([row["cmd"] for row in calls],
                         ["check_local_self_correlation", "check_correlation"])


    def test_shutdown_does_not_block_on_driver_stdout(self):
        from unittest.mock import Mock
        driver = Mock()
        driver.poll.return_value = None
        driver.stdout.readline.side_effect = AssertionError(
            "shutdown attempted unbounded stdout readline")
        gateway = Gateway()
        gateway.proc = driver
        gateway.shutdown()
        driver.stdin.write.assert_called_once_with('{"cmd":"shutdown"}\n')
        driver.stdin.flush.assert_called_once()
        driver.stdout.readline.assert_not_called()
        driver.stdin.close.assert_called_once()
        driver.stdout.close.assert_called_once()
        self.assertIsNone(gateway.proc)

    def test_shutdown_forces_cleanup_after_driver_timeout(self):
        import subprocess
        from unittest.mock import Mock
        driver = Mock()
        driver.poll.return_value = None
        # Graceful shutdown hangs. The bounded stop helper must then force
        # termination, rather than blocking indefinitely on provider I/O.
        driver.wait.side_effect = [
            subprocess.TimeoutExpired("driver", 10), 0,
        ]
        gateway = Gateway()
        gateway.proc = driver
        gateway.shutdown()
        driver.terminate.assert_called_once()
        self.assertEqual(driver.wait.call_count, 2)
        driver.stdout.readline.assert_not_called()
        self.assertIsNone(gateway.proc)


if __name__ == "__main__":
    unittest.main()
