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
| `state/store.py` | Unique-file atomic JSON publication, file fsync and POSIX directory fsync; caller owns concurrent state transaction locking |
| `integrations/model_prompts.py` | Canonical task instructions, field-evidence vocabulary and knowledge policy shared by CLI, LLM API and Antigravity SDK |
| `integrations/model.py` | `ConfiguredModelClient` transport dispatcher plus `DecisionRecorder` call/budget coordinator; no inline HTTP or CLI execution |
| `integrations/model_cli.py` | Isolated CLI Agent invocation, sandbox/thread controls and CLI response admission |
| `integrations/model_http.py` | HTTPS model API requests, headers, timeout and provider-specific response envelopes |
| `integrations/model_json.py` | Shared JSON decision decoder for CLI, HTTPS and Antigravity SDK; adapters retain their own error contracts |
| `integrations/model_metadata.py` | Context-scoped `last_usage`, model name, raw response and thread binding for direct HTTP/SDK clients and role routers |
| `integrations/model_errors.py` | Canonical ModelError shared by adapters, journal and replay |
| `integrations/model_journal.py` | Owns durable call paths, intent/terminal writes, append-only events and batch-manifest/session-index transactions |
| `integrations/model_sessions.py` | Enforces only the canonical version-2 session index with research ownership and exact per-batch call membership |
| `integrations/model_evidence.py` | Canonical evidence path derivation and symlink/traversal rejection for local replay and Journal operations |
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
- `tests/test_model_budget_concurrency.py`:
  cross-process budget transaction locks, concurrent reservations, idempotent
  verified settlement, bounded contention and atomic write crash gaps
- `tests/test_model_prompt_boundaries.py`:
  prompt consistency, SDK mock-only parity and malformed routing inputs
- `tests/test_model_journal_replay.py`:
  durable pre-call intents, unchanged manifests, crash/missing evidence and replay identity
- `tests/test_model_budget_recovery.py`:
  post-response/pre-budget-settlement crash gaps, locked idempotent settlement, unknown-outcome blockers and audit usage conflicts
- `tests/test_model_transaction_boundary.py`:
  cross-file reservation/intent/manifest/response crash gaps, sealed response
  corruption rejection and operator-visible orphan reservation IDs
- `tests/test_model_evidence_paths.py`:
  adversarial batch manifest paths, traversal, symlinks, audit/path identity,
  original input digest and unsettled-budget rejection
- `tests/test_model_evidence_toctou.py`:
  deterministic path-to-symlink swaps at audit, context, copy source and
  destination, pinned POSIX inode reads, and no-op batch bind I/O
- `tests/test_atomic_store.py`:
  interrupted rename cleanup, complete concurrent JSON publication,
  fsynced files and intact sealed model/research state
- `tests/test_model_transport_boundaries.py`:
  CLI/HTTP adapter separation, credential handling, contract decoding and no accidental cross-transport invocation
- `tests/test_agent_runtime_boundaries.py`:
  deep-copy route isolation, single metadata resolution, no stale backend identity, direct registry validation and canonical digest parity
- `tests/test_model_session_index.py`:
  strict v2 contract, cross-batch membership, unbound-call ownership,
  concurrent index merges, invalid-version rejection, corrupt references and
  manifest-to-index crash-gap repair
- `tests/test_model_cross_instance.py`:
  shared-batch deduplication, research/batch lock contention, independent
  research parallelism, multi-session manifest merging, hard-interrupt
  fail-closed replay, interrupted atomic evidence copies, audit append bounded
  waiting and OS subprocess locks
- `tests/test_agent_runtime_concurrency.py`:
  per-backend execution exclusion, independently parallel backends, concurrent lazy cache construction, per-invocation usage isolation and shared-recorder audit safety
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
- Model-call audit and budget-ledger protocols retain their current schema.
  **Response evidence now requires a canonical `state_sha256` seal in the same
  atomic JSON file.** Previously unsealed responses fail closed rather than
  being accepted by a compatibility fallback. Session indices remain strict
  v2; older index layouts are rejected, not migrated.
- The model budget ledger remains the authority for call reservations and settlements; a crash during a real provider call is not classified as a safely retriable failure.
- `DecisionRecorder` owns the execution order, task route and call budget coordination. `ModelCallJournal` exclusively owns its per-session call history and persistence paths; starting a new session clears only the in-memory history, never audit files.
- When a batch contains a persisted response, `model_replay` first checks the task/context digest, exact route, research identity, audit identity, response checksum, status, and reported usage. If governance is enabled, it additionally reconciles the original budget attempt by call ID without creating a new reservation.
- `ModelCallLedger.reconcile_verified_response` is a locked, idempotent transition: `IN_FLIGHT -> OK/ERROR`; an already matching terminal state stays unchanged, while `UNKNOWN`, a changed backend or mismatched token usage blocks. The UNKNOWN operator acknowledgement cannot be undone by replay.
- A successfully verified response may be returned without a second provider request. A persisted real-provider error is accounted for but still blocks automatic retry. Missing/malformed audit or response evidence, or a call without a durable bound batch, cannot trigger automatic reconciliation and remains operator-blocked.
- The model-call audit, ledger and route digest contracts retain their strict integrity checks. Provider response evidence is validated with `is_sealed_state` before any budget reconciliation, including when an audit or batch manifest still shows `in_flight`. No automatic conversion or legacy fallback is provided.


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

## Agent invocation concurrency contracts

- A `RoleRoutedModelClient` constructs a backend instance once under its
  short-lived factory lock and uses a **separate lock for each backend ID**
  across the full `complete_json` call and metadata capture. Two tasks
  sharing the same registered backend are serialized; separate backends
  may run concurrently even when they share the router object.
- All `last_usage`, `last_model`, `last_response_text` and
  `last_thread_id` observations are **invocation-context-local**. The
  shared `InvocationMetadata` helper provides copy-on-write ContextVar
  storage for role routers, standalone HTTPS clients and Antigravity SDK
  clients. Callers cannot mutate another invocation's usage dictionary.
  Failure before transport validation clears stale success metadata.
- `DecisionRecorder` holds a recorder-local re-entrant lock while
  switching sessions, rebinding a batch or recording an entire model
  invocation. This prevents in-memory session history and manifest writes
  from racing when one recorder is reused by multiple threads.
- These are **in-process runtime guarantees**, not a distributed provider
  request lock. The CLI's coarse-grained `state/agent.lock`, persisted
  in-flight intents and budget-ledger protections remain authoritative
  for separate processes. An unknown remote result is never silently
  retried. No model-call audit schema or route fingerprint was changed.

## Cross-instance model-call and batch ownership

- A recorder takes a **research-scoped OS invocation lock** for every call;
  when bound to a batch, it also takes that batch's **invocation lock**.
  Acquisition order is always research -> batch. These locks are held from
  before replay inspection/reservation through provider execution, global
  response/audit persistence and final budget settlement. A second instance
  fails fast before calling a provider or reserving another attempt.
- `bind_batch` takes the batch invocation lock before synchronizing historical
  session calls; a busy batch is rejected without changing the recorder's
  existing binding.
- `ModelCallJournal.sync_batch_calls` uses a **separate, short-lived batch
  manifest lock** for the read/merge/copy/write transaction and session
  index update. Independent recorder instances cannot overwrite each other's
  call IDs, and this lock is never used as a model invocation lock. Context,
  audit and response copies are first written to unique temporary files,
  fsynced and atomically replaced, so a crash during copying cannot expose
  a partially written final response file. A verified global response remains
  usable when its batch copy was never published.
- A replay always pins the original `research_id`, including when governance
  is disabled. A saved receipt may not be reused by another study merely
  because task, context and role digests happen to match.
- The global append-only audit stream uses bounded local waiting on its own
  OS file lock so *independent* studies can log events concurrently without
  failing on a transient lock collision. This waits only to persist the
  **same local event**; it is not a retry of the provider or a permission to
  resubmit an ambiguous request.
- Crashed calls remain durable as `in_flight` and cannot be automatically
  replayed without a verified response. The protected filesystem scope is
  local to the configured state directory and shared batch; distributed
  filesystems and multi-host lock semantics are not asserted.

## Current-only multi-batch Session contract

- `model_sessions/<session_id>.json` is **v2 only** with exactly
  `schema_version=2`, `session_id`, `research_id`, `call_ids` and
  `batches`. Each `batches[]` item contains `batch_ref` and its
  `call_ids`. The top-level `call_ids` is the deduplicated union.
  The redundant top-level primary `batch_ref` was deleted.
- Earlier three-field Session index layouts, absent research identity and
  unsupported schema versions are rejected immediately. No migration,
  implicit upgrade, invented history or destructive rewrite is performed.
  Operators must use a freshly initialized research state for this contract;
  old stored data is **not** deleted by the application.
- The Session index uses a filesystem lock keyed by `session_id`.
  The order is batch manifest lock -> Session index lock, held only for
  filesystem transactions and never across an external model invocation.
- Calls made before the first batch binding may be associated with one
  batch. Once attached, they cannot be copied again into another batch.
  Existing batch-owned calls are never transferred on a later rebind.
- Batch manifests are published before Session index updates. If an index
  write fails, rebind from an intact current-version index/manifest may
  repair the missing membership without requesting a model. Invalid v2
  content fails closed rather than being replaced with guessed records.
- User-supplied Session IDs and referenced call IDs must be lowercase
  32-character hex identifiers. Session IDs are bound to exactly one
  research ID. The model-call audit, replay and budget integrity policies
  are separate and continue to reject unknown outcomes.

## Model budget concurrency

- `ModelCallLedger` alone owns `reserve`, `settle`,
  `reconcile_verified_response`, `acknowledge_unknown` and `status`.
  All five perform read/validate/update under a per-research OS file lock.
- These short local transactions use bounded lock acquisition (up to two
  seconds) to tolerate overlapping writers. Contention does **not** cause
  another model invocation, retry a failed provider or allow a second
  `IN_FLIGHT` reservation; a persisted unresolved or UNKNOWN attempt
  remains blocking.
- The ledger uses atomic sealed writes. A hard interruption before the
  state replacement preserves the last valid attempt list. Verified
  duplicate reconciliation is idempotent; an ordinary duplicate settlement
  is rejected. Different research IDs use distinct lock files.
- All guarantees are for processes using the same local filesystem lock
  semantics, not multi-host distributed locking.

## Model-call cross-file transaction and crash-gap contract

- Ordering is fixed: **ledger reserve → global context/in-flight audit →
  batch manifest and intent event → provider call → atomically sealed
  response → terminal audit/event/manifest → ledger settlement**.
- The ledger and journal live in separate files and cannot be updated by
  one atomic filesystem replacement. Every intermediate crash point must
  therefore leave a fail-closed state instead of permitting automatic retry.
- The response and its `state_sha256` share **one atomic JSON write**.
  Shared `atomic_write_json` now writes to a unique same-directory temp file,
  fsyncs its contents, performs `os.replace` and fsyncs the directory on
  POSIX. Failures before replacement preserve the prior JSON unchanged.
  A terminal response persisted before the terminal audit can be accepted
  only after validating that seal, its task/context/route/research identity,
  response shape, and the original reserved budget attempt. Invalid or
  missing seals block both budget settlement and another provider request.
- A ledger reservation with no durable journal intent is an **orphaned
  reservation**, not evidence that the model call failed. `models budget`
  exposes `unresolved_call_ids` and `unknown_call_ids` for operator
  investigation without exposing prompts, credentials or response bodies.
  `acknowledge-unknown` is explicit and terminal for that research ID;
  it never refunds a call or unlocks an unverified retry.
- Checksums detect accidental corruption and incomplete copies; they are not
  cryptographic signatures against an actor able to rewrite and reseal
  local files. No BRAIN execution privilege or live model invocation is
  granted to the recovery code.

## Provider-free replay evidence path trust boundary

- The configured `state_root` and bound `batch_dir` are the only path
  authorities. `ModelReplay` reconstructs the five manifest evidence refs
  from those roots and the validated lowercase 32-hex `call_id`.
  User-editable path strings in a batch manifest are compared for equality
  and **never used to choose the file to open**. Relative paths, traversal,
  alternative filenames, symlinked files and symlinked parent directories
  are rejected before evidence is read.
- The batch manifest itself, the Session index and the global-call and
  batch evidence directories are checked before Journal reads/copies/writes.
  Canonical paths are enforced for each internal Journal evidence copy.
  Every manifest row is validated during replay and batch rebinding,
  including rows for tasks other than the current call. During replay, each
  row's call/session/task/context/route identity is checked against the global
  audit, and its original input is rehashed **before** task filtering.
  A forged task name or context digest therefore cannot turn a prior call
  into a false non-match and authorize a duplicate request.
- The global audit's `session_id`, `research_id`, `batch_ref`,
  `context_ref`, `response_ref`, task and route digest must match the
  manifest and trusted roots. The global original input JSON is also
  rehashed and checked against its recorded context SHA-256. A valid sealed
  model response is **not** by itself sufficient to settle a budget attempt
  if original input evidence is missing or altered.
- Only a validated sealed response and the exact prior budget reservation
  may be reconciled, with no provider retry on missing or suspect evidence.
  On POSIX, evidence reads walk directories with pinned `dir_fd` handles and
  `O_NOFOLLOW`; batch evidence copies also pin both source and destination
  directory handles and atomically rename within that destination directory.
  This blocks symlink substitutions between pathname validation and file
  opening. Windows rechecks canonical paths and opened file identities,
  but remains **best-effort against an adversarial concurrent replacement**:
  Python's portable file API lacks the equivalent of POSIX `openat`.
  Neither these checks nor content hashes are digital signatures.
- I/O policy: per-manifest evidence ancestry is resolved once per batch and
  global parent, then child filenames are checked lexically and with
  `is_symlink`. Descriptors recheck provenance when files are opened.
  Streaming evidence copies avoid `copy2` metadata I/O, while retaining
  content `fsync` and atomic publication. Unchanged batch rebinds must
  avoid evidence copies and JSON rewrites. Model-call and budget `fsync`
  guarantees are **not** traded away for throughput.
