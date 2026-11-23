> **历史档案**：此处保留迭代期的记录，不是当前设计或生产授权依据。请优先阅读 [整体设计](../../design/CURRENT_DESIGN.md) 和 [统一验收](../RELEASE_ACCEPTANCE.md)。

# P7.6 — Offline evidence-integrity hardening

Implementation-only follow-up to P7.3–P7.5 on `refactor/configurable-codex-agy-runtime`.

## Cross-module constraints

- The integrated gate rejects invoice reports with invalid Provider identity, missing SHA-256 batch fingerprints, invalid currency/invoice IDs, or forged independent-source assertions.
- A BRAIN report must have a coherent four-state decision table: invalid frame → scope denied; valid frame + STOPPED → Stop latched; valid frame + no monitor → monitor unavailable; valid frame + ARMED → locally audited. The admission bit and Task scope/Optimization source must agree. No state authorizes live reading.
- Canary summaries must disclose missing monitoring and irreversibly latched Stop; observed monitors require a bounded heartbeat age. When the correlated billing or BRAIN report records a negative outcome, the Canary report cannot silently omit the matching incident.
- A caller-supplied BRAIN response digest can only be correlated to an internally consistent, locally admitted offline frame report. It is never upgraded to an authenticated platform response.

## Verification debt

New negative regression tests exercise status forgery, scope drift, Provider provenance drift, omitted Canary incidents, and forged BRAIN readback flags. All test code is **added but not claimed executed here**. Queue focused P7 and full offline acceptance, then Linux/Windows/macOS CI review. No real Provider/BRAIN operation, Simulation POST, Alpha submission, or PR merge is part of this batch.

All production flags remain false, including `permission_to_dispatch`; release status remains `P7_PRODUCTION_BLOCKED`.
