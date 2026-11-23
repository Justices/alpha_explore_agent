"""P7.2 billing contract tests: offline only; no provider/BRAIN calls."""
import unittest

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_p7_billing import (
    inspect_p7_provider_invoice_batch,
)


class P7BillingTests(unittest.TestCase):
    def setUp(self):
        self.kwargs = {
            "task_id": "task-1", "provider_hostname": "api.deepseek.com",
            "invoice_id": "invoice-1",
            "rates": {"input_rate_micros_per_million": 1000000,
                      "output_rate_micros_per_million": 2000000},
            "reservations": [
                {"attempt_id": "a1", "provider_operation_id": "op1",
                 "reserved_usd_micros": 1000},
                {"attempt_id": "a2", "provider_operation_id": "op2",
                 "reserved_usd_micros": 1000}],
            "invoice_rows": [
                {"attempt_id": "a1", "provider_operation_id": "op1",
                 "input_tokens": 100, "output_tokens": 50,
                 "billed_usd_micros": 200},
                {"attempt_id": "a2", "provider_operation_id": "op2",
                 "input_tokens": 100, "output_tokens": 50,
                 "billed_usd_micros": 200}],
        }

    def run_audit(self, **changes):
        return inspect_p7_provider_invoice_batch(
            **{**self.kwargs, **changes})

    def test_exact_batch_remains_untrusted(self):
        result = self.run_audit()
        self.assertEqual(result["status"], "P7_INVOICE_MATCHED_UNTRUSTED_INPUT")
        self.assertEqual(result["operation_count"], 2)
        self.assertEqual(result["estimated_total_usd_micros"], 400)
        self.assertFalse(result["provider_invoice_independently_verified"])
        self.assertFalse(result["settlement_authorized"])
        self.assertFalse(result["budget_refund_authorized"])
        self.assertFalse(result["permission_to_dispatch"])

    def test_invoice_variance_and_reservation_breach(self):
        rows = [dict(x) for x in self.kwargs["invoice_rows"]]
        rows[0]["billed_usd_micros"] = 2000
        result = self.run_audit(invoice_rows=rows)
        self.assertEqual(result["status"], "P7_RECONCILIATION_REQUIRED")
        self.assertTrue(result["findings"][0]["reservation_breached"])

    def test_unknown_duplicate_missing_rows_fail_closed(self):
        original = self.kwargs["invoice_rows"]
        for rows in (
            original[:1],
            [original[0], original[0]],
            [original[0], {**original[1], "provider_operation_id": "alien"}],
            [original[0], {**original[1], "attempt_id": "a1"}],
        ):
            with self.subTest(rows=rows), self.assertRaises(OrchestrationError):
                self.run_audit(invoice_rows=rows)

    def test_invalid_numeric_inputs_fail_closed(self):
        for value in (True, -1, 1.1, "100"):
            rows = [dict(x) for x in self.kwargs["invoice_rows"]]
            rows[0]["input_tokens"] = value
            with self.subTest(value=value), self.assertRaises(OrchestrationError):
                self.run_audit(invoice_rows=rows)

    def test_provider_currency_and_rate_binding(self):
        for mutation in (
            {"currency": "CNY"},
            {"provider_hostname": "api.evil.example"},
            {"rates": {"input_rate_micros_per_million": True,
                       "output_rate_micros_per_million": 100}},
            {"invoice_id": ""},
        ):
            with self.subTest(mutation=mutation), self.assertRaises(OrchestrationError):
                self.run_audit(**mutation)


if __name__ == "__main__":
    unittest.main()
