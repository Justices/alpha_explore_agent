# P4.7.3 — Simulation pre-dispatch escrow and guarded write boundary

**Implementation status:** offline-only, **NOT AUTHORIZED** for production.
**Source:** `alpha_research/orchestration/pre_dispatch_escrow.py`

## Objective and non-goals

P4.7.1 stages a signed Simulation scope and a `PREPARED` Intent.
P4.7.2 verifies externally signed operator-consent *evidence* and records
a non-executable, single-use consent-audit reservation.

P4.7.3 links these existing durable records to the actual model-budget
ledger through a **pre-dispatch escrow**, but it does **not** implement
production consent, a live grant issuer, a live grant consumer, a platform
session, a chargeable model call, or a BRAIN Simulation POST.

No local file, signature, `attempt_id`, CLI flag or request parameter is
accepted as executable authorization.

## Evidence and transaction ordering

`reserve_pre_dispatch_escrow(orchestrator, task_id, governance_policy)`
checks and rechecks evidence under an OS-backed per-root writer lock:

1. The unstarted `CREATED` Super/Optimization Task, one unused orchestrator
   attempt, unchanged checkpoints, absent recovery target and stopped/Mock
   workspace guards
2. P4.7.1 canonical Simulation staging journal and exact `PREPARED` Intent;
   exact scope authorization, Task ID, Research ID, Run/Batch ID,
   request/settings hash and `dispatch_authorized=false`
3. P4.7.2 consumed consent **audit** record and its immutable
   `intent_sha256`, grant ID and operator-approval ID
4. ModelCallLedger for the staged domain Research ID under its exact
   governance policy; **zero existing model-call attempts**, no unknown
   usage, in-flight attempts or exhausted budget
5. A deterministic, operation-specific `attempt_id` SHA-256 fingerprint
   of the Task ID, grant ID, scope authorization ID, Intent hash and
   `brain_simulation` operation

It first atomically publishes a single canonical
`state/research_v1_1/simulation_predispatch_escrow/escrow_index.json`,
containing reciprocal `by_attempt`, `by_task` and `by_grant` indexes
and the full projection digest.

It then publishes one `<task_id>.json` projection with status
`PREDISPATCH_ESCROW_NOT_EXECUTABLE`, both
`permission_to_dispatch=false` and `automatic_retry_allowed=false`.

**Filesystem multi-file publication is not a single atomic transaction.**
The canonical ledger is the irreversible anti-replay claim; a crash before
the projection is persisted yields `RECONCILIATION_REQUIRED` and blocks
new escrow reservations instead of reconstructing or replaying an attempt.

The original Simulation Intent **remains PREPARED and unchanged**.
Escrow does not transition it to `POSTING`, reserve a live HTTP attempt,
or invoke any Gateway or model. This is deliberate: only a separate,
future trusted live grant consumer may implement that write boundary.

## Read-only inspection

`inspect_pre_dispatch_escrow(orchestrator, task_id, governance_policy)`
does not create directories, locks, tokens or API sessions.
It checks reciprocal indexes, projection digests and current upstream
evidence. A tampered Intent, missing index, broken projection or later
model-budget attempt becomes `RECONCILIATION_REQUIRED`.

A valid result is still `PREDISPATCH_ESCROW_NOT_EXECUTABLE`, never
`AUTHORIZED`. Status can be viewed through:

```sh
python3 -m alpha_research --lead deepseek --executor deepseek \\
  orchestration-predispatch-escrow-status <TASK_ID>
```

The unified `orchestration-admission-audit` and
`orchestration-simulation-dispatch-audit` now report the escrow status.
They continue to return `NOT_AUTHORIZED`, even for a completely
consistent escrow record.

## Two independent guarded boundaries

- `Gateway(orchestration_guarded=True)` uses a strict positive
  read-only allowlist. Every Simulation POST / resume / unknown command is
  denied before reaching subprocess driver I/O.
- The driver invoked with `--orchestration-guarded` applies its own
  positive read-only allowlist and refuses raw JSON carrying forged
  `grant_id`, `authorization_id`, `attempt_id`,
  `permission_to_dispatch=true` or `dispatch_authorized=true`.

**Existing legacy non-guarded Gateway clients are not covered** by this
new orchestration boundary. They must not be represented as production
compliance; retiring or adapting them requires separate review.

## Offline validation

`tests/test_p4_operator_consent.py` and
`tests/test_p4_guarded_dispatch.py` cover:

- Escrow requires a P4.7.2 audit reservation and intact P4.7.1 staging
- Canonical-first irreversible one-shot escrow and concurrent contenders
- Crash after index write but before per-task projection
- Model-budget activity before and after escrow, including uncertain calls
- STOP flag activated between the first and second evidence checks
- Missing index, tampered projection, mutated Intent, and negative audits
- Gateway and raw-driver refusal of forged positive authorization params
- The PREPARED Intent remains byte-for-byte unchanged after escrow
- Read-only CLI returns `NOT_ESCROWED` without filesystem writes

The CI matrix runs Ubuntu/Windows Python 3.11, macOS Python 3.11/3.13
and AGY SDK tests. CI only validates offline behavior.

## Next gates before any real Simulation

A future P4.7.4 implementation must be **separately reviewed** and have:

1. An externally provisioned authenticated operator-consent issuer and
   non-self-issued public-key trust roots, with auditable approval and
   revocation, price/currency limits and actual account provenance.
2. An independent driver-side live grant consumer that atomically claims a
   bounded, one-use operation *before* remote POST, and a trusted Gateway
   that cannot bypass this claim, even over raw stdin.
3. An end-to-end single-owner transaction protocol spanning the real
   ModelCallLedger, orchestration Task, live grant ledger, and Simulation
   Intent `PREPARED → POSTING`, with no duplicate POST and no implicit retry.
4. Operator-authorized, bounded, readback-only recovery for uncertain
   outcomes; proof of the remote operation is mandatory.
5. A separately approved, narrowly scoped canary; Alpha submission is
   independently disabled and not implied.

There is no global `--live` shortcut, no auto-signing in production and
no read-only audit result may be used as a dispatch capability.
