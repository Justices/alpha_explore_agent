# P4.7 — Canonical Simulation authorization index and evidence binding

**Status:** P4.7.1 offline implementation; **production execution remains NOT_AUTHORIZED**.
**Branch:** `refactor/configurable-codex-agy-runtime`

This phase hardens the already non-executable P4.5 Simulation staging path.
It does **not** implement a trusted consent issuer or grant consumer, call
a model, authenticate to BRAIN, run a Simulation, modify an Alpha, or submit
an Alpha. Neither an externally signed scope receipt nor a local staging
record is a production dispatch capability.

## Durable write sequence

In `alpha_research/orchestration/simulation_attempt_admission.py`, a
cross-process `SingleWriterLock` serializes staging under the state root:

1. Verify a fresh Task, immutable proposal/model identity, signed v2
   Simulation scope receipt, budget, and exact scope without posting.
2. Under the lock, revalidate the receipt, global stop flag, Mock marker,
   canonical index and existing projections.
3. Atomically write
   `state/research_v1_1/simulation_attempt_admission/authorization_index.json`
   **first**, with reciprocal `authorization_id ↔ task_id` indexes and
   the SHA-256 fingerprint of the full staging journal.
4. Write `<task_id>.json`, then create
   `intents/<task_id>.intent.json` with status `PREPARED` and
   `dispatch_authorized=false`.

The canonical index is version 2; the per-task staging journal is version 2.
Once the index claims an authorization ID or Task ID, neither may be staged
again automatically, including when a crash interrupts subsequent writes.
A partial index, missing projection, mismatched reciprocal index, altered
journal, missing Intent, or orphan evidence returns
`RECONCILIATION_REQUIRED` to read-only inspection and blocks new staging.
There is **no automatic replay, index reconstruction, cleanup or migration**.

Existing pre-P4.7 staging files without the canonical index cannot be
silently imported or interpreted as unused; they require manual review.

## Exact evidence checks

`inspect_staged_simulation_attempt` is read-only and confirms that the
canonical journal hash matches the projection, and that the local Intent
preserves workflow, Run ID, Batch ID, Research ID, request/settings digest,
single PREPARED history entry, and the exact non-executable metadata.
Missing or changed evidence never upgrades permission.

`inspect_simulation_dispatch_gate` reads the actual structured
`research_identity` saved by `MainOrchestrator.bind_research_identity`,
compares it with the staged Research ID, and reads the ModelCallLedger
without acquiring a writer lock or writing its metadata. Unknown and
in-flight model attempts are blockers. An unbound Research ID, stopped
task, altered evidence or unavailable model budget also blocks admission.

A valid read-only inspection is **not** evidence that the external platform
has authenticated a user, accepted a Simulation, or authorized a cost.
Everything stays `permission_to_dispatch=false`.

## Offline acceptance and failure injection

The Python offline suite covers:

- Durable canonical index and full journal identity fingerprint
- Cross-task replay and reciprocal-index tampering
- Process/thread contention for a single staging record
- Crash after canonical write but before the journal or Intent
- Missing canonical index with legacy/orphan records
- Intent request, Run ID, Research ID and dispatch flag drift
- Dangling symlinks and concurrent stop/Mock marker activation
- Structured Research ID binding, model-budget uncertainty and mismatch
- Inspection without filesystem repair or external calls

Cross-platform CI uses Windows and Ubuntu Python 3.11, macOS Python 3.11
and 3.13, plus the AGY SDK contract check. Passing CI confirms only the
tested offline behavior, not BRAIN connectivity or production authorization.

## Separate remaining release gates

1. A genuine trusted operator-consent issuer with key custody, scoped one-shot
   authorization, positive approval evidence and revocation/expiry.
2. A production-grade, atomically consumed live grant with stable operation
   identity, durable attempt journal and independently guarded Gateway/driver.
3. Integration of exact Task, Research, ModelCallLedger and Simulation Intent
   identities without budget double-spend, duplicate POST or blind retries.
4. Operator-authorized, bounded readback/recovery tests and explicit canary
   controls, with independent Alpha submission approval.

`ProductionWorkflowAdapter` and guarded Gateway writes remain disabled.
Do not add a global `--live` override, infer approval from staging, or
interpret `READ_ONLY_SCOPE_VERIFIED` as permission to simulate.
