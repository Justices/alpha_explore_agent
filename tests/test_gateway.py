import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nastu_quant.gateway import Gateway, GatewayError


class TestGateway(unittest.TestCase):
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
