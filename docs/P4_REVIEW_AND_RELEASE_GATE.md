# P4 code review, remediation and release gates

**Branch:** `refactor/configurable-codex-agy-runtime`
**Verification policy:** Static source inspection and code changes only in
this P4 round. No manual unit tests, live model calls, BRAIN Simulations or
Alpha submissions were initiated. Workflow CI may run automatically on push;
successful CI for an earlier commit is not evidence for this change set.

## P4.1 — Implemented P0/P1 remediation

| Priority | Risk identified | Change | Verification |
|---|---|---|---|
| P0 | Mock records could be persisted without a canonical origin check | RecordStore enforces `OFFLINE_DOMAIN_MOCK` in marked workspaces; rejects mock origin in an unmarked workspace | Test code authored; execution deferred |
| P0 | A live Gateway could be passed directly to domain Dispatch with a marked mock state root | Dispatch rejects live Gateway and model; CLI RuntimeContext and ReadOnlyEvidenceGateway enforce the boundary independently | Test code authored; execution deferred |
| P0 | A corrupted execution-receipt ledger could be treated as valid if unrelated entries disagree | Validate all by-task/by-ID mappings, reject any mismatch | Test code authored; execution deferred |
| P0 | A missing receipt ledger with surviving projections for another task could be reconstructed empty | Refuse reservation and report reconciliation, for any orphan projection | Test code authored; execution deferred |
| P1 | Process-global RecordStore monkey-patching could leak Mock provenance into unrelated threads | Replace with ContextVar-based `offline_mock_record_scope`; restore token on exit | Test code authored; execution deferred |
| P1 | Mock marker could be claimed concurrently after an evidence check | OS lock and evidence recheck under lock; reject conflicting writer | Test code authored; execution deferred |
| P1 | Admission audit did not incorporate every non-executable reservation ambiguity | Include signed receipt reservation state and reject corrupt records, stop flags, bound research and recovery target | Test code authored; execution deferred |
| P1 | A malformed mock record could raise `TypeError` before normal record validation | Stamp only dictionary records, preserving canonical validation errors | Test code authored; execution deferred |

### Cross-module invariants

- `state/research_v1_1/domain_mock_workspace.json` is a permanent marker,
  never a permission token.
- A mock marker prohibits live CLI commands, direct domain dispatch with a
  live Gateway, active CLI runtime handoff with a live model, and live read-only
  catalog bridges.
- Mock evidence is stamped in a **context-local** scope and cannot be written
  to unmarked roots by the canonical record store.
- The CandidatePool on a marked root is always empty; outside it, explicit
  `OFFLINE_DOMAIN_MOCK` rows are excluded from index and reuse decisions.
- The execution receipt verifier does not sign approvals, obtain user consent,
  authenticate to BRAIN or reserve live execution.
- `RESERVED_INSPECTION_ONLY` is durable and single-use but can never dispatch
  a model, a simulation or Alpha submission.
- The disabled ProductionWorkflowAdapter remains fail-closed.

## P4.2 — Regression files written (not executed)

- `tests/test_p3_mock_isolation.py`: marker/legacy evidence collisions,
  context-local origin, cross-thread isolation, direct dispatch/runtime guards.
- `tests/test_p4_execution_reservation.py`: one-shot reservation, missing
  canonical ledger, orphan projections for unrelated tasks, mismatched
  indices, mock-root refusal.
- Existing prepared-task/domain-Mock/production-adapter tests must be run as
  part of the entire suite; tests around simulated candidate pools deserve
  special attention following the new provenance constraints.

**Do not interpret test files being committed as a passing test run.**

## P4.3 — Production-readiness gates

### Available as read-only inspection

- `orchestration-prepare`: immutable proposal and model identity; never
  authorizes a provider.
- `orchestration-admission-audit`: returns `NOT_AUTHORIZED` with specific
  blockers (task, budget, policy, review, Mock workspace, stop flag,
  signed-receipt ledger).
- `orchestration-authorization-inspect`: validates an externally signed
  receipt, returns `INSPECTED_NOT_EXECUTABLE`.
- `orchestration-authorization-reserve`: persists one-shot audit only,
  `RESERVED_INSPECTION_ONLY`.

### Still necessary before production execution

1. A trusted authorization issuer with actual user-consent and purpose
   verification, distinct from the local verifier secret.
2. An actual atomic *live* admission/attempt journal. The existing receipt
   audit ledger is deliberately not sufficient.
3. An approved CLI-owned Gateway/SDK handoff that enforces operation-specific
   grants just before each BRAIN/model mutation.
4. Durable pre-POST simulation intent, recoverable readback and manual
   reconciliation of unknown provider outcomes without replay.
5. End-to-end proof of ModelCallLedger accounting and real workflow/budget
   correlation.
6. Separate Alpha submission authorization, quotas and verification of
   RA/PC/SC and PPA selection policy.
7. Static, offline, CI cross-platform and operator-authorized canary acceptance.

These features cannot be called production-ready without those external
authorization boundaries and actual testing. Do not add `--live` or a
global boolean to bypass the admission protocol.

## Release acceptance plan (deferred per operator)

1. Compile/import check and repository-wide static lint/type review, including
   circular imports, task/receipt errors and Python 3.11/3.13 syntax.
2. Run the offline test suite on Linux, Windows and macOS 3.11/3.13.
3. Run specific malformed signed-receipt, cross-task anti-replay and
   unknown-outcome tests with networking blocked.
4. Verify no paid model/BRAIN request is made in any P4 offline command.
5. Review errors/failures, fix, rerun, and record an exact commit SHA for the
   accepted test result.
6. Only after separate explicit user authorization consider a bounded
   read-only platform integration test. Simulation/submission remain
   independently gated.

**Exit status for this code-only round: source changes committed,
verification pending, live execution disabled.**
