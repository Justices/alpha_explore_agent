> **历史档案**：此处保留迭代期的记录，不是当前设计或生产授权依据。请优先阅读 [整体设计](../../design/CURRENT_DESIGN.md) 和 [统一验收](../RELEASE_ACCEPTANCE.md)。

# P7.4 — Canary monitoring, watchdog decision and stop policy

P7.4 provides a **pure, offline supervision decision engine**, not a deployed watchdog or automatic irreversible control action. It composes the existing P5.5 persistent Stop and monitor with P7 billing/BRAIN audit states.

## Inspection API

`inspect_p7_canary_supervision(task_id=..., monitor=..., observed_at=..., previous_monitor=..., previous_observed_at=..., billing_report=..., brain_report=..., now=..., max_age_seconds=90)`

The inputs are bounded local monitor snapshots matching the P5 schema. Timestamps must include an offset and satisfy the heartbeat limit. Previous monitoring snapshots are optional but must be paired with timestamps.

The decision engine catches:

- missing or stale telemetry, observation times in the future, lost global cap and persistent Stop;
- any P5 monitor alert, UNKNOWN Provider attempt, at-least-80%-committed budget, counter rollback and modified immutable spending cap;
- STOPPED reverting to ARMED or changing its recorded event, a mismatched BRAIN stop snapshot, denied BRAIN scope, and P7 billing variance;
- Task identity and negative authority-contract drift.

Statuses are `P7_CANARY_OFFLINE_OBSERVATION_ONLY`, `P7_CANARY_STOP_REQUIRED`, or `P7_CANARY_STOP_ALREADY_LATCHED`. All outputs include independent decision reasons, lineage digests, `admin_stop_invoked=false`, `watchdog_executed=false` and `permission_to_dispatch=false`.

## Emergency response

The independent P5.5 Authority's admin-only `/v1/canary/stop` operation is the existing *irreversible* emergency control and is transactionally checked against new budget reservations. If an approved operator receives STOP_REQUIRED from an authenticated telemetry channel, the operator must investigate and invoke this existing admin control through the secure authority client. No local audit report automatically invokes Stop, retries a paid call or refunds a reservation.

This distinction is intentional: the local monitor is not signed remote production telemetry, and the code cannot prove that an external Provider or BRAIN process honors a stop. A real Canary requires a separately deployed observer, trusted transport, alert delivery, incident runbook and operational authority.

## Deferred acceptance

Test STOP-before-reserve, restart persistence, stale heartbeat, UNKNOWN outcome, ledger rollback and Stop resurrection. Run `python3 -m unittest tests.test_p7_integrated_release -v` plus P5.5 and P6 suites after code optimization. Only an independently approved operator may perform a real supervised canary.
