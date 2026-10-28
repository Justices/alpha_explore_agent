import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nastu_quant.gateway import Gateway, GatewayError


class TestGateway(unittest.TestCase):
    def test_read_only_session_failure_is_returned_without_gateway_retry(self):
        gateway = Gateway()
        gateway.proc = type("Process", (), {"poll": lambda self: None})()
        sent = []
        gateway._send = lambda payload: sent.append(payload) or {
            "ok": False, "error": "401 Unauthorized"}
        with self.assertRaisesRegex(GatewayError, "401 Unauthorized"):
            gateway.call("get_datasets", region="GLB")
        self.assertEqual([item["cmd"] for item in sent], ["get_datasets"])

    def test_expired_session_never_reposts_simulation(self):
        gateway = Gateway()
        gateway.proc = type("Process", (), {"poll": lambda self: None})()
        sent = []
        gateway._send = lambda payload: sent.append(payload) or {
            "ok": False, "error": "401 Unauthorized"}
        with self.assertRaisesRegex(GatewayError, "401 Unauthorized"):
            gateway.call("simulate_batch", variants=[{"expression": "rank(x)"}])
        self.assertEqual([item["cmd"] for item in sent], ["simulate_batch"])

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

    def test_composite_submission_check_is_not_exposed(self):
        gateway = Gateway()
        with self.assertRaises(GatewayError):
            gateway.call("get_submission_check", alpha_id="A1")


if __name__ == "__main__":
    unittest.main()
