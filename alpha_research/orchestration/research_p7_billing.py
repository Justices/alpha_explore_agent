"""P7.2 offline provider invoice reconciliation.

A caller-supplied invoice is NOT independently authenticated provider billing.
No SDK, credentials, network, retries, ledger mutation, or release permission.
"""
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_model_cost_boundary import ceil_cost_usd_micros
from alpha_research.state.fingerprint import fingerprint_json


def _nonnegative_int(value, name):
    if type(value) is not int or value < 0:
        raise OrchestrationError("P7 billing invalid " + name)
    return value


def inspect_p7_provider_invoice_batch(*, task_id, provider_hostname,
                                      rates, reservations, invoice_rows,
                                      invoice_id, currency="USD"):
    """Reconcile a complete, exact operation set; fail closed on uncertainty.

    reservations: [{attempt_id, provider_operation_id, reserved_usd_micros}]
    invoice_rows: [{attempt_id, provider_operation_id, input_tokens,
                    output_tokens, billed_usd_micros}]
    No partial matches, duplicate IDs, unsupported currency or unknown rows.
    """
    if not isinstance(task_id, str) or not task_id.strip():
        raise OrchestrationError("P7 billing task identity missing")
    if provider_hostname not in (
            "api.deepseek.com", "open.bigmodel.cn", "api.moonshot.cn"):
        raise OrchestrationError("P7 billing provider identity invalid")
    if currency != "USD" or not isinstance(invoice_id, str) or not invoice_id.strip():
        raise OrchestrationError("P7 billing invoice identity invalid")
    if not isinstance(rates, dict) or set(rates) != {
            "input_rate_micros_per_million", "output_rate_micros_per_million"}:
        raise OrchestrationError("P7 billing rate contract invalid")
    for field, value in rates.items():
        _nonnegative_int(value, field)
    if (not isinstance(reservations, list) or not reservations
            or not isinstance(invoice_rows, list)
            or len(reservations) != len(invoice_rows)
            or len(reservations) > 10000):
        raise OrchestrationError("P7 billing requires bounded complete invoice")
    reserved_by_key = {}
    reservation_attempts = set()
    for item in reservations:
        if not isinstance(item, dict) or set(item) != {
                "attempt_id", "provider_operation_id", "reserved_usd_micros"}:
            raise OrchestrationError("P7 billing reservation schema invalid")
        attempt, operation = item["attempt_id"], item["provider_operation_id"]
        if (not isinstance(attempt, str) or not attempt
                or not isinstance(operation, str) or not operation
                or len(attempt) > 256 or len(operation) > 256
                or attempt in reservation_attempts
                or operation in reserved_by_key):
            raise OrchestrationError("P7 billing duplicate reservation identity")
        reservation_attempts.add(attempt)
        reserved_by_key[operation] = (attempt, _nonnegative_int(
            item["reserved_usd_micros"], "reserved_usd_micros"))
    seen = set()
    billed_total = estimated_total = reserved_total = 0
    findings = []
    for item in invoice_rows:
        required = {"attempt_id", "provider_operation_id", "input_tokens",
                    "output_tokens", "billed_usd_micros"}
        if not isinstance(item, dict) or set(item) != required:
            raise OrchestrationError("P7 billing invoice row schema invalid")
        operation, attempt = item["provider_operation_id"], item["attempt_id"]
        if operation not in reserved_by_key or operation in seen:
            raise OrchestrationError("P7 billing unknown or duplicate operation")
        expected_attempt, reserved = reserved_by_key[operation]
        if attempt != expected_attempt:
            raise OrchestrationError("P7 billing attempt/operation mismatch")
        seen.add(operation)
        inp = _nonnegative_int(item["input_tokens"], "input_tokens")
        out = _nonnegative_int(item["output_tokens"], "output_tokens")
        billed = _nonnegative_int(item["billed_usd_micros"], "billed_usd_micros")
        estimate = ceil_cost_usd_micros(
            input_tokens=inp, output_tokens=out,
            input_rate_micros_per_million=rates["input_rate_micros_per_million"],
            output_rate_micros_per_million=rates["output_rate_micros_per_million"])
        billed_total += billed
        estimated_total += estimate
        reserved_total += reserved
        findings.append({
            "attempt_id": attempt, "provider_operation_id": operation,
            "reserved_usd_micros": reserved,
            "estimated_usd_micros": estimate,
            "billed_usd_micros": billed,
            "variance_usd_micros": billed - estimate,
            "reservation_breached": billed > reserved or estimate > reserved,
        })
    if seen != set(reserved_by_key):
        raise OrchestrationError("P7 billing invoice operation set incomplete")
    findings.sort(key=lambda row: (row["attempt_id"], row["provider_operation_id"]))
    discrepancy = any(row["variance_usd_micros"] != 0 or
                      row["reservation_breached"] for row in findings)
    return {
        "schema_version": 1, "task_id": task_id,
        "status": ("P7_RECONCILIATION_REQUIRED"
                   if discrepancy else "P7_INVOICE_MATCHED_UNTRUSTED_INPUT"),
        "provider_hostname": provider_hostname, "invoice_id": invoice_id,
        "currency": currency, "operation_count": len(findings),
        "reserved_total_usd_micros": reserved_total,
        "estimated_total_usd_micros": estimated_total,
        "billed_total_usd_micros": billed_total,
        "variance_total_usd_micros": billed_total - estimated_total,
        "findings": findings,
        "invoice_rows_sha256": fingerprint_json(invoice_rows),
        "reservation_rows_sha256": fingerprint_json(reservations),
        "provider_invoice_independently_verified": False,
        "independent_invoice_source_authenticated": False,
        "settlement_authorized": False, "budget_refund_authorized": False,
        "automatic_retry_allowed": False,
        "real_provider_invoked": False, "permission_to_dispatch": False,
    }
