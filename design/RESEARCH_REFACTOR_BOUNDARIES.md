# Research orchestration refactoring boundaries

This document describes implemented module ownership after the Regular
ResearchAgent extraction. It is a record of shipped boundaries, not an
authorization to call BRAIN or to promote/submit an Alpha.

## Current boundaries

| Component | Responsibility |
| --- | --- |
| `workflows/regular/agent.py` | Regular workflow coordinator; owns gateway permissions, preregistration, simulation readback and recovery |
| `workflows/regular/errors.py` | Canonical Regular ResearchError |
| `workflows/regular/field_catalog.py` | Catalog response admission, Dataset-scoped field pool and filtering |
| `workflows/regular/settings_validation.py` | Strict platform test-period evidence verification |
| `workflows/regular/decisions.py` | Knowledge-enriched JSON decisions and local audit references; no BRAIN API access |
| `workflows/regular/field_plan.py` | Per-field construction/repair and child-batch sequencing; delegates calls and recovery to the coordinator |
| `workflows/regular/candidate_audit.py` | Canonical platform audit, RA/correlation checks and retained-Candidate revalidation |
| `workflows/regular/case_evidence.py` | Case pool and Alpha state persistence, immutable remedy lineage validation |
| `orchestration/engine.py` | Main workflow task state machine, budget reservations and receipt/path-verified reconciliation |
| `orchestration/offline_adapter.py` | Both explicitly offline adapters; never the live BRAIN gateway |
| `state/fingerprint.py` | Canonical JSON digest |
| `state/sealed.py` | One persisted state checksum contract for both offline research checkpoints and model-call ledgers |
| `integrations/model_prompts.py` | Canonical task instructions, field-evidence vocabulary and knowledge policy shared by CLI, LLM API and Antigravity SDK |
| `integrations/model.py` | `ConfiguredModelClient` transport dispatcher plus `DecisionRecorder` call/budget coordinator; no inline HTTP or CLI execution |
| `integrations/model_cli.py` | Isolated CLI Agent invocation, sandbox/thread controls and CLI response admission |
| `integrations/model_http.py` | HTTPS model API requests, headers, timeout and provider-specific response envelopes |
| `integrations/model_json.py` | Shared JSON decision decoder for CLI, HTTPS and Antigravity SDK; adapters retain their own error contracts |
| `integrations/model_errors.py` | Canonical ModelError shared by adapters, journal and replay |
| `integrations/model_journal.py` | Single owner of session call history, durable call paths, intent/terminal writes, append-only events, batch manifests and session indices |
| `integrations/model_replay.py` | Validated batch response replay, route/audit identity checks, corruption and unknown-outcome rejection |
| `agents/backend_validation.py` | Provider families, backend safety admission and transport-specific privileges; never invokes providers |
| `agents/configuration.py` | Strict role/task binding and defensively copied route snapshots; delegates backend policy |

## Execution authority

The model/Agent only proposes or attributes research. Regular's original
authoritative Gateway path still controls `set_case`, `simulate_batch`,
`resume_simulation_batch`, canonical readback, RA, PC/SC, Candidate checks
and recovery. Components extracted from ResearchAgent do not grant additional
permissions or bypass its persisted Simulation Intent.

Do not treat `CandidateAuditService` as a generic model-evaluated Candidate
gate: it is still backed by live platform evidence. The component deliberately
supports a lightweight testing instance with no ResearchAgent constructor side
effects to preserve deterministic audit-policy tests.

## Distinct state machines still preserved

`MainOrchestrator` is the task-level control plane for Regular, Super and
Optimization; `DurableOfflineResearchRunner` is a bounded, fixture-only
research-round test engine. They are not equivalent state machines: the latter
requires the exact `OfflineSimulationGateway` type and never executes
real Simulation. This refactor **does not** replace MainOrchestrator with the
fixture engine or automatically migrate historical saved state.

Both now use the same canonical state digests and sealed checkpoint helpers.
Each still validates its own schema, research identity, scope, completion
status, execution budgets and recovery permissions. Any future migration
between engines requires a versioned state migration and crash-gap tests;
no implicit fallback or repost of an unknown Simulation is permitted.

## Verification

- `tests/test_regular_components.py`: extracted contract boundaries
- `tests/test_research_agent.py`: Regular preregistration, field workflows,
  recovery and readback
- `tests/test_brain_flow_integration.py`: RA, platform audit, correlation,
  retained candidate behavior
- `tests/test_durable_research_cycle.py`: fixture-only research journal
- `tests/test_orchestrator.py`, `tests/test_workflow_adapter.py`:
  MainOrchestrator state machine and offline adapter
- `tests/test_model_governance.py`, `tests/test_sealed_state.py`:
  persistent model budgets, crash intent and corruption handling
- `tests/test_model_prompt_boundaries.py`:
  prompt consistency, SDK mock-only parity and malformed routing inputs
- `tests/test_model_journal_replay.py`:
  durable pre-call intents, unchanged manifests, crash/missing evidence and replay identity
- `tests/test_model_budget_recovery.py`:
  post-response/pre-budget-settlement crash gaps, locked idempotent settlement, unknown-outcome blockers and audit usage conflicts
- `tests/test_model_transport_boundaries.py`:
  CLI/HTTP adapter separation, credential handling, contract decoding and no accidental cross-transport invocation
- `tests/test_agent_runtime_boundaries.py`:
  deep-copy route isolation, single metadata resolution, no stale backend identity, direct registry validation and canonical digest parity
- `tests/test_orchestrator.py`:
  includes receipt-path containment and nonstring journal-path rejection before any external file read
- `tests/test_orchestration_concurrency.py`:
  task write-CAS and execution locks, actual child-process OS locking,
  competing recovery handlers, stale lock files and hard-interrupt crash gaps

Run `python -m unittest discover -s tests -v` without BRAIN credentials.
Never enable real Simulation or Alpha submission merely to validate a refactor.

## Model call recovery invariants

- Before contacting any configured Provider, DecisionRecorder reserves the attempt (when governance is enabled), persists the input and an in-flight audit record, then creates the batch manifest.
- `ModelCallJournal` owns only local durable metadata and never receives provider credentials or permission to use BRAIN.
- `model_replay` accepts only previously recorded successful JSON decisions bound to the task, context digest and exact role/backend route digest. Invalid manifests and missing or tampered responses must block rather than trigger automatic real-provider retries.
- The persisted file names, manifest structure and event status vocabulary remain unchanged by this refactor; a separate versioned migration would be necessary to modify persisted semantics.
- The model budget ledger remains the authority for call reservations and settlements; a crash during a real provider call is not classified as a safely retriable failure.
- `DecisionRecorder` owns the execution order, task route and call budget coordination. `ModelCallJournal` exclusively owns its per-session call history and persistence paths; starting a new session clears only the in-memory history, never audit files.
- When a batch contains a persisted response, `model_replay` first checks the task/context digest, exact route, audit identity, response checksum, status, and reported usage. If governance is enabled, it additionally pins the research ID. Only then does it reconcile the original budget attempt by call ID without creating a new reservation.
- `ModelCallLedger.reconcile_verified_response` is a locked, idempotent transition: `IN_FLIGHT -> OK/ERROR`; an already matching terminal state stays unchanged, while `UNKNOWN`, a changed backend or mismatched token usage blocks. The UNKNOWN operator acknowledgement cannot be undone by replay.
- A successfully verified response may be returned without a second provider request. A persisted real-provider error is accounted for but still blocks automatic retry. Missing/malformed audit or response evidence, or a call without a durable bound batch, cannot trigger automatic reconciliation and remains operator-blocked.
- The model-call file schema, ledger schema version and recorded route digest structure remain unchanged; changes to these require a dedicated migration and crash-gap fixtures.


## Routing and recovery trust boundaries

- The model registry validates each backend directly through the same
  `validate_backend` policy used by the live router; registry inspection must
  never construct a synthetic role or invoke a provider.
- `RoleRoutingConfig.resolve` returns a **deep copy** of the effective
  backend definition. Mutating a selected route cannot alter later dispatch.
  `DecisionRecorder` requests one route snapshot for audit identity rather
  than independently looking up role, backend name and provider configuration.
- `RoleRoutedModelClient` clears previously active backend metadata before
  resolving each new invocation. An invalid task cannot leak another role's
  model name or token usage into the current error context.
- Domain recovery and read-only model evidence retain their distinct
  authorization boundaries but use `state/fingerprint.py` for identical
  finite-JSON checksums; no persisted receipt schema or digest changes.
- `MainOrchestrator.reconcile` checks a recovery receipt's digest and rejects
  a journal path outside the configured recovery root **before reading** the
  referenced journal. Invalid reference types also fail closed. The
  `RecoveryCoordinator` remains the only producer of valid reconciliation
  receipts; no recovery code may repost an unknown Simulation.

## Cross-process control-plane invariants

- The CLI still owns `state/agent.lock` as the coarse-grained operator lock.
  Direct `MainOrchestrator` callers additionally acquire a separate
  **task-scoped execution lock** spanning the entire injected Adapter call.
  A competing executor or reconciliation request fails before it may
  misclassify the active step as an unknown outcome.
- Every orchestration create/update uses a short-lived **task write lock**:
  version comparison and atomic JSON replacement occur in the same critical
  section. Recovery-target callbacks may use the write lock while their
  owning execution still holds the task execution lock; these are different
  locks intentionally, and they are not nested recursively.
- `RecoveryCoordinator.recover` holds a **target-scoped recovery lock**
  through its journal check, intent write and handler invocation. This
  prevents two processes from both observing an absent journal and running
  the same external recovery handler. The target is re-inspected under lock.
- `recover_task` also holds a separate **task-scoped recovery lock** across
  domain recovery, regular campaign reconciliation and the verified task
  receipt write. This lock must be different from the execution lock because
  task `reconcile()` acquires that execution lock itself.
- Lock contention is **fail-fast**: it cannot be reclassified as a provider
  failure, cause an automatic Simulation repost or consume extra task budget.
  OS lockfiles may remain after crashes but do not represent ownership:
  the OS lock is authoritative. A crash during execute or recovery leaves
  the saved RUNNING intent for explicit reconciliation, not automatic retry.
- This guarantee is scoped to processes observing the **same local filesystem
  locks**. No network-filesystem/distributed lock semantics are assumed.
  Task checkpoint and model budget ledgers remain distinct state machines.
