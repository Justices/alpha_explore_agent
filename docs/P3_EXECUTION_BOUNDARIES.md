# P3: execution and evidence boundaries

**Status:** Implementation committed; verification deliberately deferred by operator.
**Scope:** Regular Alpha, Super Alpha, Optimization. This document describes
code contracts, not a claim that live provider execution is approved.

## 1. Workspaces and evidence origin

- The default research state root is never accepted by
  `orchestration-domain-mock`.
- A new domain mock requires a dedicated `--state-root` without existing
  campaign, candidate, preferred, simulation or platform evidence. Path and
  marker validation reject symlinked state parents, marker and record roots.
- The first mock invocation writes
  `state/research_v1_1/domain_mock_workspace.json` with canonical root
  identity and `OFFLINE_DOMAIN_MOCK_ONLY` mode.
- Subsequent mock invocations can reuse a correctly marked mock root, using
  **new prepared task IDs**. An unmarked state root containing evidence fails
  closed; no automatic conversion or migration is attempted.
- All ResearchRecordStore writes executed inside the mock domain boundary
  receive `evidence_origin: OFFLINE_DOMAIN_MOCK`, including candidate,
  preferred and campaign records. The canonical store refuses unstamped
  records in a marked workspace and refuses stamped records in an unmarked
  workspace; the store rejects bypasses through direct domain calls.
- CandidatePool.build_index and CandidatePool.find_reusable exclude records
  with mock provenance. A marked mock workspace produces **no** live pool
  entries, even if legacy/raw files lacking provenance are copied into it.
  Mock research artifacts remain inspectable, never promotable.
- Existing live CLI workflow commands (including real recovery, research
  planning, tagging and model probes with explicit paid-call flags) reject a
  marked offline mock root.
- A fixture-only `orchestration-run` operates on orchestration checkpoints,
  not domain evidence. Its `SYNTHETIC_ONLY` status is never a candidate verdict.

**Non-goal:** This does not retroactively label files from older mock runs.
Never point a new mock workflow at a production root or manually copy mock
records into a production root. A future import boundary must explicitly
validate provenance before any research record migration.

## 2. Prepared execution contracts

`orchestration-prepare` persists an immutable proposal and a pinned model
identity. A default config is usable only for offline preparation; it cannot
become implicit production authorization. The proposal and identity both
carry `allow_model_call=false`, `allow_brain_simulation=false`,
`allow_alpha_submission=false`.

`orchestration-admission-audit TASK_ID` is a **read-only, negative audit**:
it checks current task and budget, saved proposal and identity hashes, model
profile/policy drift, config source, mock workspace status and review ledger
status. It returns `NOT_AUTHORIZED` with explicit blockers. It does not
create a writer lock, model client, Gateway session, model attempt or Simulation
Intent.

A signed `review_execution_proposal` receipt is **review only**; it does
not grant invocation rights. Even a valid receipt cannot make the disabled
ProductionWorkflowAdapter executable.

`orchestration-authorization-inspect TASK_ID --receipt PATH` validates an
**externally issued** execution receipt under a separate HMAC-SHA256 signing
context and verification environment variable
`ALPHA_RESEARCH_EXECUTION_VERIFY_KEY`. The exact receipt binds Task ID,
Workflow, request/proposal SHA-256, selected model-profile SHA-256, model
governance SHA-256, one enumerated operation, single attempt, UTC issue/expiry
and at most 15-minute lifetime. The program does **not** issue receipts, does
not accept local self-signing as approval, does not reserve an execution grant,
and does not consume the receipt for dispatch. Even a valid signature returns
`INSPECTED_NOT_EXECUTABLE` with all permission booleans false.

This strict parser and verifier is only a prerequisite for a future trusted
authorization service; key possession alone is not proof of actual user
consent. ProductionWorkflowAdapter remains disabled.

## 3. Future production admission (not implemented)

Before enabling the first live BRAIN operation, independently review and
implement all of the following:

1. A trusted **execution** approval issuer, distinct from review-only
   approval. The signed receipt verification contract is now implemented,
   but the trusted issuer and independent consent/permission check are not.
2. An atomic admission journal and single-writer reservation before any
   model or BRAIN I/O; reconcile crashes without refunding unknown attempts.
3. Gateway ownership, credentials and provider routing transferred only from
   the authorized CLI runtime; no untrusted adapter or arbitrary dispatcher.
4. One idempotency/recovery contract per BRAIN write, including durable
   Simulation Intent **before** POST and no automatic retry on unknown outcome.
5. Research ID to ModelCallLedger identity binding, provider usage reporting
   and scoped call budget accounting.
6. Independent review of Alpha Candidate gates, PPA eligibility, submission
   quota and an explicit user authorization for submissions.
7. No production admission when offline provenance, a mock workspace marker,
   missing/ambiguous identity, unavailable evidence or a stale policy is
   detected.

Until all gates exist and are separately verified, production execute
**must remain disabled**. No boolean `live=true` bypass is permitted.

## 4. Deferred verification matrix

The user requested no manual testing in this implementation round.
Before release, run the complete offline matrix on Linux/Windows/macOS,
including Python 3.11 and 3.13, and validate these assertions:

- Fresh dedicated mock root accepts all three workflows; default root and
  preexisting evidence root reject the command before task mutation.
- Every Mock candidate and preferred row has provenance; direct record-store
  calls cannot bypass the stamp; the pool and reuse cache reject it, including
  unstamped raw files in a marked mock workspace.
- Mock workspace denies all live research, recovery, tagging and model smoke
  commands, including explicit `--allow-model-call`.
- Proposal, profile and policy drift reject mock and admission operations.
- No real BRAIN, paid model, Alpha submission or unapproved Simulation occurs.
- Unknown Simulation result leaves `BLOCKED`, retains reserved budget,
  and cannot automatically replay POST.
- Admission audit creates no runtime state and remains `NOT_AUTHORIZED`,
  including when a review-only receipt is valid.
- Signed execution receipt parsing rejects malformed envelopes, timestamps,
  signing keys, policy drift and replay conditions, yet never enables dispatch.
- Native macOS temporary-path aliases and symlink/TOCTOU attack cases
  still fail closed (not just canonical `TMPDIR` happy path).

## 5. Operator CLI

```bash
nq --state-root "$STATE_ROOT" --lead deepseek --executor deepseek \
  orchestration-prepare --workflow regular --region CHN \
  --objective "offline P3 review"

nq --state-root "$STATE_ROOT" --lead deepseek --executor deepseek \
  orchestration-admission-audit <TASK_ID>

# Only in a clean disposable, non-default root:
nq --state-root "$MOCK_STATE_ROOT" --lead deepseek --executor deepseek \
  orchestration-domain-mock <NEW_MOCK_TASK_ID> --mode mock
```

Do not run the last command on an established production workspace.
