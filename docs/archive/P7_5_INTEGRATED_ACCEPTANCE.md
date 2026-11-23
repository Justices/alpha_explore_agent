> **历史档案**：此处保留迭代期的记录，不是当前设计或生产授权依据。请优先阅读 [整体设计](../../design/CURRENT_DESIGN.md) 和 [统一验收](../RELEASE_ACCEPTANCE.md)。

# P7.5 — Cross-module acceptance and production release gate

**Result:** the repository now has a single read-only P7 release matrix, explicitly incapable of granting production or live execution authorization.

## Integration architecture

`inspect_p7_integrated_release(orchestrator, task_id, *, billing_report=None, brain_report=None, canary_report=None)`

- Starts from the existing P6 negative release baseline and carries all P5/P6 blockers forward.
- P7.1 reports only its isolated local deployment contract; it does not assert independently deployed infrastructure.
- P7.2 checks operation/attempt uniqueness, integer micro-USD totals, row variances and reservation breach verdicts before accepting a supplied invoice audit; no invoice record is independently authenticated.
- P7.3 verifies the constrained negative signed-session report and exact BRAIN frame hashes, not a real BRAIN session.
- P7.4 checks incident status, Stop semantics and the SHA-256 lineage between its billing and BRAIN inputs; conflicting reports fail closed.

Missing reports yield explicit blockers. Present reports that contradict Task identity or the negative permission contract raise `OrchestrationError`. An offline invoice match or ARMED local monitor cannot remove external release blockers.

The existing `inspect_production_release_gate` includes `research_p7_acceptance` for Super/Optimization and the one-query control-plane status now surfaces `p7_production_status` and `p7_production_blockers`. Existing P6 fields are preserved.

## Production blockers (external, not code-completable)

1. Independent production PKI, identities, authority deployment, audit storage, recovery, watchdog and on-call ownership.
2. Authenticated Provider invoice and per-operation usage readback, tariff versioning and hard spending limits.
3. Real scoped, authenticated, revocable BRAIN read-only session with independently attested request/response binding.
4. Supervised operator-approved Canary with stop propagation confirmed at each live execution endpoint.
5. Separate BRAIN Simulation POST authorization; automatic Alpha submission remains forbidden by the project contract.

These blockers cannot be resolved with local test fixtures, self-signed reports, mock SDK responses or GitHub CI. Status is **`P7_PRODUCTION_BLOCKED`**, and all dispatch and mutation flags remain false.

## Validation queue

After the code batches are complete:

```bash
python3 -m compileall -q alpha_research tests
python3 -m unittest tests.test_p7_billing -v
python3 -m unittest tests.test_p7_integrated_release -v
python3 -m unittest discover -s tests -v
```

Then review Linux, Windows, macOS CI; inspect the P7 release report and all failures before further changes. No offline test authorizes external credentials, actual BRAIN Simulation, Alpha submission or paid model calls.
