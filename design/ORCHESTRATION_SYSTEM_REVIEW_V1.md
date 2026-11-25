# Alpha Research Orchestration System Review V1

> Review date: 2026-11-25  
> Target branch: `refactor/configurable-codex-agy-runtime`  
> Scope: system-level workflow orchestration, not module-local cleanup.  
> This review is not an execution grant. The current one-shot Regular pilot boundary remains authoritative.

## 1. Executive finding

The repository has substantial domain capabilities and durable control-plane components, but the three business workflows are not yet delivered through one complete production orchestration path.

The central gap is not a missing Agent. It is the incomplete integration between:
- the task-level `MainOrchestrator`;
- workflow-owned domain execution and qualification;
- model-attempt and Simulation-intent ledgers;
- external authorization and recovery;
- campaign-level feedback and research-memory updates.

The existing code deliberately fails closed at these boundaries. This is correct safety behavior, but it means the system must not be described as a generally production-ready autonomous research platform.

## 2. Evidence-based current topology

```text
CLI / Workbench
  |
  +-- MainOrchestrator (Task state, checkpoints, task budget, recovery receipt)
  |     |
  |     +-- ProductionWorkflowAdapter
  |     |     +-- validates / projects existing qualification
  |     |     +-- refuses real execute
  |     |
  |     +-- LocalRegularPilotAdapter
  |     |     +-- reuses active CLI runtime and real Regular domain dispatch
  |     |     +-- one Dataset, one batch, at most one Simulation POST
  |     |     +-- binds Research ID and recovery target to Task
  |     |
  |     +-- AdmittedCliDomainAdapter / LiveDomainBridge
  |           +-- Regular-only live bridge
  |           +-- requires independent one-use Authority consumer
  |
  +-- Existing domain CLI workflows
  |     +-- Regular Campaign (real domain workflow)
  |     +-- Super native workflow (separate domain path)
  |     +-- Optimization branch workflow (separate domain path)
  |
  +-- OfflineResearchRuntimeAdapter
        +-- scripted Mock model + Mock BRAIN catalog only
        +-- Super / Optimization rehearsal, not live execution
```

### Verified ownership boundaries

- `MainOrchestrator` owns task identity, task checkpoints, execution budget and receipt-verified reconciliation.
- Regular domain workflow owns its campaign, field plan, Simulation intent, platform readback, RA/correlation audit and domain recovery.
- Super and Optimization have distinct platform/domain contracts; they must not be coerced into Regular expression or Dataset semantics.
- Model Agent/LLM proposes hypotheses and decisions. Deterministic domain code and platform evidence own admission, execution state and qualification.
- `project_existing_qualification` is intentionally a projection of domain-owned qualification. It does not perform a second audit or issue new platform calls.
- Production authorization, model-provider billing and BRAIN Simulation are separate authority/ledger concerns. Local hashes and Mock/CI evidence do not replace independent attestation.

## 3. Target end-to-end research orchestration

The target is one campaign-level orchestrator with workflow-specific domain strategies and shared evidence/control contracts.

```text
Research Request
  |
  v
Campaign Planner
  |-- preserve user hard constraints
  |-- resolve Region / Dataset / Category / Delay / Universe
  |-- create bounded work plan and explicit coverage obligations
  v
Workflow Router
  |-- Regular (+ optional PPA profile)
  |-- BRAIN-native Super
  |-- Alpha Optimization (source snapshot + independent branch)
  v
Domain Research Strategy
  |-- evidence/catalog acquisition
  |-- economic hypothesis and falsifiable rationale
  |-- field/operator or native Super construction plan
  |-- expression/parameter validation
  v
Model Decision Boundary
  |-- registered Lead / Executor role routing
  |-- immutable context, route identity, attempt and spend reservation
  |-- verified response or UNKNOWN; never blind retry
  v
Execution Admission
  |-- exact scope and settings
  |-- durable pre-registration / Simulation Intent
  |-- separate operation-specific authority and budget
  v
Workflow Executor
  |-- domain-specific BRAIN Gateway / Driver contract
  |-- bounded Simulation operations
  v
Readback & Reconciliation
  |-- platform operation / Alpha identity
  |-- exact expression and settings
  |-- immutable response receipt and evidence lineage
  |-- UNKNOWN remains blocked pending independent readback
  v
Result Attribution
  |-- metrics, RA failures, diagnostics and failure signatures
  |-- distinguish platform facts from model hypotheses
  v
Research Decision
  |-- improve / branch / stop / retain for audit
  |-- explicit iteration and resource budgets
  v
Qualification
  |-- RA terminal PASS and fail count = 0 first
  |-- then local self-correlation prefilter
  |-- then platform PC / SC and complete applicable audit
  |-- Candidate only after all required evidence passes
  v
Candidate Pool -> Preferred Pool -> Research Memory
  |                                   |
  +----------- feedback --------------+
```

The three workflows share orchestration, state, evidence, budgets, recovery and reporting. They do not share domain semantics where the platform contracts differ.

## 4. System-level gaps (frozen for this review)

### P0 — Current task reconciliation and operation identity
1. The active Regular pilot must reconcile the existing model attempt against its original call ID, route, context digest, sealed response, usage evidence and reserved budget.
2. The Simulation READ_BACK must be bound to the original Simulation intent, exact task/batch and platform receipt. It does not settle the model ledger.
3. No new provider call, Simulation POST, replay, retry, refund, or new task may be used to make the current evidence look complete.

### P1 — Unified workflow lifecycle
1. There is no single production adapter that safely coordinates the three domain workflows end to end.
2. The current Local Regular pilot is a deliberately narrow bridge; the current offline Super/Optimization adapter is not a live adapter.
3. Task-level `execute` currently encloses the domain execution as one coarse step. The architecture needs explicit domain event/checkpoint handoffs for planning, model attempts, intent, dispatch, readback, attribution and qualification, without merging their separate state machines.
4. Workflow completion, Candidate qualification and Preferred selection must remain distinct terminal facts.

### P1 — Research feedback loop
1. Model-attributed failure explanations must be recorded as hypotheses, not platform facts.
2. Improvement decisions must reference the exact source result, diagnosis, scope and changed expression/parameters.
3. A new branch or iteration must consume an explicit research budget and must not silently expand scope.
4. Stop conditions must distinguish budget exhaustion, evidence pending, platform rejection, repeated failure and genuine research exhaustion.

### P1 — Campaign-level planning and coverage
1. Regular multi-Dataset Campaigns need parent/child identity, independent Dataset subtask status, coverage obligations and result lineage.
2. Super planning must use the BRAIN-native mechanism and its own supported input schema.
3. Optimization must pin a read-only source snapshot and keep every modified candidate in an independent branch.
4. The scheduler must make serial/parallel policy explicit; multiple Dataset scope does not imply permission for parallel execution.

### P2 — Research memory and portfolio learning
1. Persist verified positive and negative evidence with validity, scope, Dataset, mechanism and policy fingerprints.
2. Reuse prior evidence only when the exact applicability contract matches.
3. Do not promote model-written explanations or unreviewed patterns into verified remedies.
4. Feed retained Candidate/Preferred and failure evidence back into future planning without changing historical records.

## 5. Implementation sequence (system modules, not micro-optimizations)

| Order | System work package | Completion evidence |
|---|---|---|
| 1 | Current task model-attempt reconciliation | Original attempt terminal state and budget are reconciled from trusted evidence; no provider replay |
| 2 | Orchestration event/checkpoint contract | One task timeline links planning, model attempt, domain intent, dispatch, readback, attribution and qualification |
| 3 | Shared workflow adapter contract | Regular/Super/Optimization adapters expose common lifecycle events while retaining domain-specific state machines |
| 4 | Campaign planner and scheduler | Parent/child Dataset tasks, explicit coverage, budget and stop semantics |
| 5 | Feedback and improvement coordinator | Diagnosis-to-change lineage, bounded iterations, stop/branch decisions |
| 6 | Qualification and candidate lifecycle | RA-first order, correlation gating, audit evidence, Candidate then Preferred |
| 7 | Research memory integration | Verified evidence reuse and invalidation, no speculative promotion |
| 8 | End-to-end offline acceptance | Cross-workflow contract tests and failure-injection tests bound to the current commit |
| 9 | Separately authorized live validation | Only the exact scope and operation budget approved by the operator |

## 6. Current execution boundary

The currently approved live validation remains exactly:
- one supervised Regular pilot;
- the already existing model attempt must be reconciled before any continuation;
- at most one BRAIN Simulation batch for that pilot;
- no Alpha submission or Alpha property/tag mutation;
- no retry or replay of uncertain Provider/BRAIN operations;
- no additional Simulation;
- no live Super or Optimization;
- no production release claim.

Offline code changes and offline tests may proceed only when they do not mutate or reinterpret the active pilot's evidence, widen its authority, or enable a new live execution path.

## 7. Definition of system-level Done

The system is complete only when:
1. every workflow has an explicit end-to-end orchestration contract and domain adapter;
2. Campaign/Task/Research/Model Attempt/Simulation Intent/Candidate identities are linked without collapsing their state machines;
3. all external side effects have durable intent, scoped authority, budget reservation, readback and recovery;
4. feedback loops have bounded iteration, provenance and deterministic stop rules;
5. Candidate and Preferred states are derived from complete applicable evidence;
6. the workbench reports actual backend task state and evidence, not browser-only placeholders;
7. cross-platform offline acceptance passes on the exact commit;
8. live readiness is established by independent external evidence and explicit operator authorization, not by code or local reports alone.

No additional cleanup item should be added to this review unless it changes one of these completion claims or fixes a demonstrated correctness/security defect.
