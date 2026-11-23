> **历史档案**：此处保留迭代期的记录，不是当前设计或生产授权依据。请优先阅读 [整体设计](../../design/CURRENT_DESIGN.md) 和 [统一验收](../RELEASE_ACCEPTANCE.md)。

# P7.2 — Provider Token and invoice reconciliation (offline)

P7.2 implements **a bounded offline billing reconciliation module**, not a live Provider invoice integration. It is intentionally grouped as one business module: operation and attempt linkage, exact invoice-set admission, conservative integer micro-USD re-estimation, reservation checks, variance reporting and integration into the existing negative-only production release gate.

## Input contract

Call `inspect_p7_provider_invoice_batch` from `alpha_research.orchestration.research_p7_billing` with an exact Task ID, one registered Provider hostname, immutable invoice identity, currency USD, signed-price-equivalent rate integers, reserved operation rows and invoice rows. Its strict, bounded schema refuses unknown, missing or duplicate operations; duplicated attempts; mismatched operation/attempt pairs; noninteger or negative tokens, costs or rates; mixed providers and non-USD currency.

The function correlates rows by the pre-recorded Provider Operation ID and attempt ID, recalculates integer ceiling cost from reported input/output tokens and pinned rates, records per-row and aggregate invoice variance and detects invoice/estimate amounts above reserved capacity. It includes canonical hashes of supplied rows for downstream audit correlation.

**Trust boundary:** every invoice row is caller-supplied, not read from a Provider API. An exact match therefore yields `P7_INVOICE_MATCHED_UNTRUSTED_INPUT`, never a verified invoice or a settlement authorization. Mismatch yields `P7_RECONCILIATION_REQUIRED`. Missing/duplicate/malformed records raise a fail-closed exception instead of returning an incomplete success.

`inspect_p6_integrated_release(..., p7_billing_report=report)` incorporates these conclusions without weakening prior P6 metering discrepancies. The release remains `P6_PRODUCTION_BLOCKED` with independently verified Provider billing explicitly false. No refund of reserved funds, no retry and no positive permission can be inferred.

## Deferred external integrations

A later independently approved deployment must obtain the complete invoice directly from an authenticated Provider billing source, validate account/credential custody, date/period/currency, tariff effective dates, provider-specific token categories and pagination, independently prove record completeness and reconcile credit limit or actual ledger settlement. This P7.2 module intentionally does not accept a source-authenticated flag that callers could spoof.

## Acceptance queue (not executed during code optimization)

- `python3 -m unittest tests.test_p7_billing -v`
- Existing P6/P5 contract and production gate regression suites
- Full offline unit/integration suite and Linux/Windows/macOS acceptance
- Confirm live Provider/BRAIN request counts remain zero

**Production remains blocked.**
