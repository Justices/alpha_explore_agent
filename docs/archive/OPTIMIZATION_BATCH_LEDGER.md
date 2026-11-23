> **历史档案**：此处保留迭代期的记录，不是当前设计或生产授权依据。请优先阅读 [整体设计](../../design/CURRENT_DESIGN.md) 和 [统一验收](../RELEASE_ACCEPTANCE.md)。

# Alpha research — four-batch code optimization ledger

Updated: 2026-11-23. Scope: branch `refactor/configurable-codex-agy-runtime`.

## Workflow rule

**One business module per batch.** Optimize the four modules and then run a dedicated cross-module code review; perform
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

## Batch 5 — Cross-module architectural review and code cleanup

The final review is focused on the contracts and interactions among the four
previous batches. It does not add a fifth live execution path.

- **Release status correctness:** P6 reporting now distinguishes a valid
  offline token-cost estimate from `RECONCILIATION_REQUIRED`; a BRAIN
  catalog frame rejected by the scope gate is reported as
  `READ_ONLY_FRAME_DENIED`, not "policy verified." Additional blocker
  codes preserve these negative outcomes in the top-level report.
- **Readback cannot overwrite failure:** a signed provider operation
  observation can be valid while its estimate is still inconsistent.
  The P6 release matrix now retains the prior metering
  `RECONCILIATION_REQUIRED` status, regardless of a signed observation.
  Signed readback validation requires explicit no-live/no-retry claims.
- **Subprocess lifecycle:** Gateway shutdown no longer calls a blocking
  response read on a potentially unresponsive child. It first requests
  graceful termination and enforces bounded wait/kill/pipe cleanup.
- **Dependency cleanup:** removed a known-unused `ipaddress` import in
  TLS, `GUARDED_FORBIDDEN_CLAIMS` import in Gateway and unused
  `MainOrchestrator` import in research preparation. No public API or
  persistence schema was deleted.

### Deferred validation and regression risk register

These are **not yet proved by tests in this optimization pass**:

- Grant consumption and global Stop ordering across simultaneous
  connections/processes and broker errors
- Full provider identity matrix, pricing rounding and unchanged mock SDK
  outcome/operation custody
- Driver bootstrap, read-only protocol, graceful/hung subprocess shutdown,
  pipe failure cleanup and Windows/macOS process behavior
- P6 release matrix: rejected BRAIN frames, over-ceiling usage, invoice
  mismatch, signed readback with simultaneous unresolved metering and
  forged positive authorization claims
- Control-plane compatibility, sealed journal recovery and all previous
  Regular, Super, Optimization, PPA and candidate-selection behavior

The previous P6 CI result (922 tests) predates **all** these optimization
commits and must not be used to claim this batch has passed.

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

## P7.3–P7.5 — Complete module implementation handoff

- **P7.3 BRAIN read-only protocol:** P4.26 signed Task/ACK session observation, strict P4.17 frame allowlist, P5 independent Stop gate, bounded batch uniqueness and caller-supplied response lineage. No BRAIN credential/session is opened, and no live read permission is granted.
- **P7.4 Canary supervision:** P5 monitor schema, fresh heartbeat, STOPPED latch, UNKNOWN attempts, budget alarms, monotonicity and cross-report drift feed deterministic stop decisions; admin Stop is not invoked automatically.
- **P7.5 integrated acceptance:** P7.2 integer billing arithmetic, P7.3 signed-session frame evidence, P7.4 Stop telemetry and P6 negative blockers are correlated; top-level release gate and research control-plane status include P7.5. Production is always blocked.

Implementation files: `research_p7_brain_read.py`, `research_p7_canary.py`, `research_p7_release.py`, `research_p7_billing.py`; regression modules: `test_p7_billing.py` and `test_p7_integrated_release.py`.

### Acceptance debt (do not mark as validated without actual run)

- Compile/import/static checks for the changed files and package layout.
- P7.2–7.5 focused offline tests and P4/P5/P6 compatibility.
- Complete Regular/Super/Optimization research, recovery and PPA suites.
- Linux/Windows/macOS CI, dependency versions, no-live-no-retry assertions.
- Independent real Provider invoices, authenticated BRAIN read sessions and supervised Canary are **external deployment prerequisites**, not code-optimization tasks.

The implementation batch is handed off for unified offline acceptance. No live Provider request, BRAIN Simulation, Alpha submission, PR merge or authorization change has been performed.
