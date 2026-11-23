"""Pure shared P7 contract tests, no live calls or credentials."""
import unittest

from alpha_research.orchestration.research_p7_contracts import (
    BILLING_STATUSES, BRAIN_STATUSES, CANARY_STATUSES, PROVIDER_HOSTNAMES,
    brain_read_status, canary_status,
)


class P7ContractTests(unittest.TestCase):
    def test_brain_all_six_state_combinations(self):
        expected = {
            (False, "ARMED"): "P7_BRAIN_READ_SCOPE_DENIED",
            (False, "STOPPED"): "P7_BRAIN_READ_SCOPE_DENIED",
            (False, "NOT_SUPPLIED"): "P7_BRAIN_READ_SCOPE_DENIED",
            (True, "ARMED"): "P7_BRAIN_READ_FRAME_AUDITED_NO_LIVE",
            (True, "STOPPED"): "P7_BRAIN_READ_STOP_LATCHED",
            (True, "NOT_SUPPLIED"): "P7_BRAIN_READ_MONITOR_UNAVAILABLE",
        }
        for args, status in expected.items():
            with self.subTest(args=args):
                self.assertEqual(brain_read_status(*args), status)
                self.assertIn(status, BRAIN_STATUSES)

    def test_canary_stop_precedes_healthy_report(self):
        for stopped in (True, False):
            for required in (True, False):
                status = canary_status(stopped, required)
                self.assertIn(status, CANARY_STATUSES)
                if stopped:
                    self.assertEqual(status, "P7_CANARY_STOP_ALREADY_LATCHED")

    def test_provider_and_invoice_vocabularies(self):
        self.assertEqual(len(PROVIDER_HOSTNAMES), 3)
        self.assertEqual(BILLING_STATUSES, {
            "P7_INVOICE_MATCHED_UNTRUSTED_INPUT", "P7_RECONCILIATION_REQUIRED",
        })


if __name__ == "__main__":
    unittest.main()
