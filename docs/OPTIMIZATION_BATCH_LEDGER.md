# Alpha research — four-batch code optimization ledger

Updated: 2026-11-23. Scope: branch `refactor/configurable-codex-agy-runtime`.

## Workflow rule

**One business module per batch.** Optimize all four modules first; perform
static checks, offline unit/integration tests and cross-platform acceptance
only after the code batches are complete. This ledger tracks *implementation
changes*, not evidence of successfully executed tests. The repository's
existing push-triggered GitHub Actions are separate from any manually
requested final acceptance; no CI result has been claimed for these commits.

No real LLM charges, BRAIN Simulation POST, Alpha submission, or PR merge
are authorized by this code optimization work. All production dispatch
remains explicitly `PRODUCTION_BLOCKED`.

## Batch 1 — Independent authority and infrastructure

- Grant consumption and irreversible global emergency stop now serialize
  under the **same `BEGIN IMMEDIATE` SQLite transaction**, closing the
  pre-consumption Stop-to-consumption race. Per-Grant model spending already
  performs its Stop check atomically.
- Startup configuration is parsed **once**, with strict duplicate-key and
  nonfinite-number rejection; TLS material is all-or-none to avoid a
  partial-option silent downgrade. Operator/recovery trust documents are
  bounded and parsed through a single helper.
- The loopback HTTP service rejects duplicate authorization and malformed
  duplicated envelope headers. Global and per-Grant spend metrics come
  from one consistent, policy-verified SQLite read transaction.

Remaining external work: independently managed CA/key custody and runtime
identity, actual deployment, monitoring, provider billing, operational
supervision.

## Batch 2 — Model provider and spending

- The preflight, local SDK MockTransport observation, P6 billing estimate,
  and independent service reservation use the **same conservative integer
  micro-USD formula**, with no floating-point rounding divergence.
- Provider hostname, API base path, and `*_API_KEY` env variable are
  validated as a **single backend identity contract** for DeepSeek, GLM, Kimi;
  mixed-provider configurations fail closed.
- The real OpenAI-compatible SDK adapter applies the registered backend
  schema again at runtime, before loading credentials, and rejects mismatched
  model responses and incomplete `finish_reason` values.

Remaining external work: provider-verified tokenization, pricing, invoice
readback, hard provider ceilings and independently approved paid dispatch.
A signed local readback alone does **not** settle billing.

## Batch 3 — BRAIN Gateway/Driver

- The narrow read-only probe command allowlist is owned by a single module
  and imported by both the Gateway and the independent driver.
- Untrusted truthy mode settings no longer silently coerce into read-only or
  guarded execution. Driver rejects oversized protected input frames and
  redacts protected-mode provider errors.
- Failed startup and shutdown now perform bounded child termination,
  process reaping and stream cleanup, including credential-hand-off errors.

Remaining external work: independently authenticated scoped BRAIN research
read-only session, verified current platform Catalog evidence and controlled
live read authorization. Simulation/Alpha writes need separate approvals.

## Batch 4 — Research orchestration and recovery

- Model-attempt and preparation journal parsing is size-bounded and
  centralized in the sealed recovery paths, preserving `UNKNOWN` semantics
  and never retrying a non-idempotent operation automatically.
- The one-query research control-plane status includes the P6 production
  readiness verdict and blockers. It still distinguishes incomplete
  preparation, recovery uncertainty, local candidate evidence and the
  absence of external production authority.

Remaining external work: real BRAIN-native Super / Regular / Optimization
evidence and independently authorized Simulation; complete candidate gates
and preferred selection must remain separate from submission. Automated
Alpha submission is still disallowed by project contract.

## Final verification queue — NOT YET EXECUTED FOR THIS OPTIMIZATION BATCH

1. Static compilation, imports and config/schema validation.
2. Domain-specific offline tests for all four modules, including Stop/Grant
   race, provider-URL identity, micro-USD rounding and driver command scope.
3. Complete offline suite including Regular, Super, Optimization, PPA and
   recovery scenarios.
4. Cross-platform Linux, Windows, macOS CI; reconcile every failure.
5. Final diff review and explicit report of unverified production blockers.

This queued acceptance phase should only begin **after** all desired code
optimization changes are complete; passing CI cannot substitute for live
provider/BRAIN authorization.
