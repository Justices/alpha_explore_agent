> **历史档案**：此处保留迭代期的记录，不是当前设计或生产授权依据。请优先阅读 [整体设计](../../design/CURRENT_DESIGN.md) 和 [统一验收](../RELEASE_ACCEPTANCE.md)。

# P5.5 — Offline Canary, global emergency stop, and negative release gate

**This is a local pilot and offline integration contract, not a live Canary.**
The P5.1–P5.4 independent local authority gained persistent emergency-stop
and passive monitoring. The BRAIN research driver, paid SDK dispatch, Alpha
Simulation POST and Alpha submission remain **not authorized**.

## Service-owned global stop

The independent authority initializes one `research_canary_control`
record in the same SQLite file that stores one-time Grants, spend reservations
and signed recovery observations. The record begins `ARMED` for the
**local offline mock only**. ARMED is **not** a production release decision.

An operator with the separate admin token invokes:

```http
POST /v1/canary/stop
Authorization: Bearer <ADMIN_TOKEN>
Content-Type: application/json

{}
```

This commits a unique immutable stop event and changes the global state
to STOPPED. **There is no reset API.** Repeated stop requests fail; the
record survives service restart and schema initialization. A new pilot
environment must be deliberately provisioned with a separate database.

Budget reservations check the stop **within the same BEGIN IMMEDIATE
transaction** as the spend-limit check and reservation insertion. The stop
itself uses BEGIN IMMEDIATE, making the stop and reservations serializable.
A reservation that committed before a stop may still exist; its money
remains committed, and after the stop new attempts cannot reserve more.

The offline SDK mock rechecks the stop after reserving and before
MockTransport execution. If it sees STOPPED, the existing attempt is
durably quarantined as UNKNOWN, with no retry and no capacity release.
This recheck is **not a provider-atomic dispatch authorization**: it must
never be wired to a real provider by changing a flag or URL. Real
execution would require an independent enforcement point with atomic
admission, identity and kill-switch semantics.

History, operation-ID lookups, quarantine and admin-signed outcome
observations remain possible **after** a stop (and even after Grant
revocation), to support incident investigation without new execution.

## Passive telemetry and readiness

The authenticated `POST /v1/canary/monitor` endpoint accepts only `{}`
and returns aggregate, deliberately redacted counters:

- current stop state, stop event ID and timestamp;
- consumed/revoked Grant counts, attempt counts by RESERVED/UNKNOWN/SETTLED;
- total **committed reservation** micros and immutable global spend cap;
- signed recovery observation count, plus alert strings:
  `GLOBAL_STOP_LATCHED`, `UNRESOLVED_PROVIDER_ATTEMPTS`,
  `GLOBAL_RESERVED_BUDGET_AT_LEAST_80_PERCENT` and/or
  `GLOBAL_SPEND_POLICY_NOT_INITIALIZED`.

The service does not expose API credentials, prompts, model responses,
full receipts or raw signed observations in these metrics. These are
local observations, **not signed production telemetry**.

The authenticated `POST /v1/canary/preflight` verifies a consumed,
unexpired Task/Grant ACK and the local stop state. Its only affirmative
claim is `offline_mock_permitted=true`; it explicitly returns
`permission_to_dispatch=false`,
`real_model_dispatch_authorized=false` and
`brain_read_authorized=false`. It cannot launch a live Canary.

The integrated `research_p5_release.py` gate and the existing top-level
`inspect_production_release_gate` assemble an explicit list of remaining
blockers. Even a valid local monitor, signed P4 reviews, and an ARMED stop
state **cannot** authorize live execution. Malformed or positive-permission
monitor assertions fail closed.

## Service endpoints

| POST endpoint | Role | Behavior |
| --- | --- | --- |
| `/v1/canary/stop` | Admin | Irreversibly latch independent GLOBAL stop |
| `/v1/canary/monitor` | API | Read-only redacted telemetry and alerts |
| `/v1/canary/preflight` | API | Verify exact Task/ACK and stop for offline SDK MockTransport only |
| `/v1/spend/reserve` | API | Atomically checks GLOBAL stop before a new reservation |
| `/v1/spend/quarantine` | API | Preserve ambiguous attempts after stop |
| `/v1/spend/attempt` | API | Read existing attempt after stop |
| `/v1/spend/reconcile` | Admin | Append signed audit-only remote observation |

Bind only 127.0.0.1. Use distinct high-entropy API/admin tokens, as
documented in [P5 local authority setup](P5_RESEARCH_AUTHORITY_LOCAL_PILOT.md).

## Offline acceptance

```bash
python3 -m pip install -e .
python3 -m unittest discover -s tests -p 'test_p4_research_final_release.py' -v
python3 -m unittest discover -s tests -p 'test_p4_release_gate.py' -v
python3 -m unittest discover -s tests -v
```

Tests cover stop-before-Grant, stop-after-reserve, reinit/restart,
separate admin token, zero-data monitoring, 80%-reserved and UNKNOWN
alerts, SDK stop between reserve and mock invocation, and the always
negative P5 release gate.

## What remains to reach a supervised *real* Canary

This pilot **does not satisfy** production prerequisites:

1. Independently operated TLS/mTLS client/server identity and credential
   custody, including operator-approved Canary scope.
2. Real BRAIN authenticated read-only Driver and SDK service-side
   enforcement, separate from Simulation POST and Alpha submission.
3. Provider-specific tokenization/price ceilings, hard spending caps,
   independently authenticated provider operation readback and invoice
   reconciliation; signed test observations are not invoices.
4. Trusted stop propagation to every real execution process, watchdog,
   metrics/alert routing and operator incident response.
5. Controlled supervised trial, real account approval, roll-back strategy
   and separate sign-off to enable live calls.

Production release remains `PRODUCTION_BLOCKED`.
