# Workflow-First Delivery Plan

> Decision: establish end-to-end business flow first; converge modules second; reorganize the codebase third.
> Branch: `refactor/configurable-codex-agy-runtime`
> Date: 2026-11-25

## 1. Delivery principle

Do not optimize module boundaries ahead of proving that the complete research workflow works.

The order is:

1. **Workflow completion:** connect the existing components into observable, resumable end-to-end business flows. Keep current safety boundaries and domain state machines.
2. **Module convergence:** after real execution paths and handoffs are proven, identify duplicated responsibilities and converge contracts, adapters, state ownership and shared services.
3. **Codebase organization:** only after ownership is stable, remove dead/duplicate code, rename packages, reorganize directories and finalize documentation/deployment.

During phase 1, make the smallest changes needed to connect a missing handoff or fix a demonstrated flow blocker. Do not perform speculative broad refactors.

## 2. End-to-end acceptance flows

### A. Regular Alpha (including optional PPA profile)

```text
Campaign request
 -> pin Region / Dataset(s) / Category / Delay / Universe
 -> persist the existing Campaign parent, work items and per-Dataset subtasks
 -> acquire and review field/operator evidence
 -> construct economic hypotheses and expressions
 -> validate expressions and simulation settings
 -> reserve model attempt and persist Simulation intent
 -> execute bounded Simulation
 -> read back exact platform result
 -> run RA audit (RA fail count must be zero before correlation)
 -> run applicable correlation and remaining qualification checks
 -> record Candidate or explicit rejection/pending state
 -> analyze failure/success and decide improve / stop / retain
 -> persist lineage and update research memory
```

PPA is a Regular profile, not an independent workflow or a bypass around Regular admission and qualification.

Acceptance: one pinned Dataset vertical slice first; then multi-Dataset parent/child campaign behavior; then bounded improvement iterations. A one-batch pilot is not proof of multi-Dataset campaign completion.

### B. Super Alpha

```text
Super request
 -> pin supported native Super scope/settings
 -> inspect platform-native Super inputs and constraints
 -> formulate and validate native Super hypothesis
 -> reserve model attempt and operation authority
 -> execute bounded native Super Simulation
 -> read back exact result and reconcile intent
 -> apply Super-specific RA / PC / SC and other applicable checks
 -> record Candidate / Preferred / rejected / pending
 -> attribute result and decide improve / stop / retain
 -> persist lineage and research memory
```

Acceptance: use the platform's native Super mechanism and schema. Do not route Super through Regular Campaign or claim the offline scripted adapter is a live implementation.

### C. Specified Alpha Optimization

```text
Optimization request + source Alpha ID
 -> read and pin immutable source Alpha snapshot
 -> inspect source expression, settings, metrics and audit evidence
 -> formulate bounded modification hypotheses
 -> create independent optimization branch
 -> validate changed expression/settings
 -> reserve model attempt and operation authority
 -> execute bounded optimization Simulation
 -> read back exact result and reconcile intent
 -> compare source and branch under equivalent scope
 -> run required qualification and correlation checks
 -> retain Candidate / Preferred / rejected / pending
 -> preserve source unchanged; record lineage and research memory
```

Acceptance: source Alpha remains immutable; every variant is a separately identified branch; comparisons are reproducible and scope-equivalent.

## 2.1 Existing implementation to evolve (no greenfield redesign)

Phase 1 must build on these existing components and contracts:

| Existing component | Existing responsibility | Evolution rule |
|---|---|---|
| `orchestration/engine.py:MainOrchestrator` | Durable Task lifecycle, four checkpoints, task budget, task/recovery locks and receipt-based reconciliation | Keep it as the task-level control plane; add only the missing handoffs needed to trace the actual workflow |
| `orchestration/dispatch.py:dispatch` | Routes to the existing Regular Campaign, native Super workflow and Optimization workflow | Keep this as the existing domain dispatch boundary; do not create a parallel workflow implementation |
| `orchestration/offline_adapter.py:MockWorkflowAdapter` | Invokes the real dispatcher with explicitly offline Gateway/Model implementations | Use it for end-to-end offline vertical slices; never treat it as a live adapter |
| `workflows/regular/campaign.py` | Existing Campaign planning, work-item schedule, per-Dataset progress/review, bounded batch execution and campaign summary | Extend the existing Campaign lifecycle and preserve its persisted records/subtasks |
| `workflows/super_alpha/workflow.py` | Existing BRAIN-native Super preparation, proposal, Simulation Intent, readback and qualification | Preserve native Super schema and workflow-specific lifecycle |
| `workflows/optimization/workflow.py` | Existing immutable source snapshot, independent candidate branch, Simulation Intent, source-integrity check, readback and qualification | Preserve source/branch lineage and existing optimization records |
| `orchestration/local_regular_pilot.py` | Supervised live Regular bridge with a strict one-Dataset/one-batch bound | Keep the existing bound; do not generalize it to Super/Optimization or multi-batch live dispatch |
| `orchestration/research_runtime_adapter.py` | Scripted offline Super/Optimization preparation/runtime | Keep it explicitly offline until each domain's own live admission and recovery path is verified |
| `orchestration/workflow_contract.py` | Shared request mapping and projection of domain-owned qualification | Extend only where the existing workflow contract is demonstrably incomplete; do not duplicate domain qualification |
| `orchestration/event_journal.py` + `execution_trace.py` | New append-only per-Task hash-chained event journal and read-only cross-record trace projection | Link existing Task, Campaign, Model Journal, Simulation Intent, Readback, Candidate and Recovery evidence; never duplicate their business state or store raw prompts/responses |

Important interpretation: Regular Campaign's `max_batches` is a per-Dataset attempt limit in the existing Campaign implementation. With `max_batches=1`, a Campaign containing multiple verified Dataset work items can still execute one batch for each Dataset. The supervised Local Regular Pilot separately requires exactly one Dataset and one batch. Do not confuse the Pilot limit with the Campaign's overall multi-Dataset semantics.

The current `MainOrchestrator` has four coarse task checkpoints (`validate`, `execute`, `qualify`, `collect`). Domain workflows already persist finer-grained records such as Campaign state, Research identity, model-call journal, Simulation Intent, response/readback, audit and Candidate/Preferred records. The new per-Task event journal is an append-only correlation layer: it records lifecycle milestones, IDs, safe evidence references and hashes, while the existing domain records remain authoritative. `orchestration-execution-trace` verifies the event hash chain and projects the related evidence read-only. Phase 1 should link and expose these existing records before introducing any new state machine or replacing domain-owned persistence.

## 3. Shared end-to-end contracts (do not prematurely merge domain state)

All three workflows must expose enough evidence for the campaign controller to observe:

- request and immutable scope identity;
- ordered Task event trace with a verified hash chain, stage transitions and domain milestone references;
- workflow-specific plan and validation result;
- model attempt identity, route, context digest, usage/budget settlement and UNKNOWN state;
- domain Research/Campaign/branch identity;
- Simulation intent, operation reservation, platform operation identity and readback receipt;
- diagnosis/attribution with platform facts distinguished from model hypotheses;
- qualification evidence and explicit Candidate/Preferred decision;
- next action, iteration budget, stop reason and recovery target.

These are shared observation and handoff contracts. They do not require a single universal domain state machine.

## 4. Phase gates

### Phase 1 — Make workflows run end to end

For each workflow, produce a traceable vertical slice with:
- one request accepted and scope pinned;
- each stage handoff persisted and inspectable;
- domain execution and platform evidence linked to the task;
- qualification result or explicit evidence-pending/rejected outcome;
- a final next-action decision (improve, stop, retain, or await reconciliation);
- restart/recovery behavior that never blindly replays an unknown external operation;
- a read-only trace command that verifies and correlates Task events, model-call journals, session indexes, domain records, Simulation Intent/Readback, Candidate and Recovery evidence.

Start with the narrowest Regular vertical slice because a supervised local real Regular path already exists. In parallel, complete offline end-to-end traces for Super and Optimization, then wire their live adapters only behind their own verified domain and external-authority contracts.

Do not claim the whole system is end-to-end complete merely because one Regular pilot succeeds.

### Phase 2 — Converge modules

Only after the flow traces identify stable handoffs:
- merge genuinely duplicated orchestration responsibilities;
- establish canonical ownership for Task, Campaign, Research, Model Attempt, Simulation Intent and Candidate records;
- unify event/checkpoint and adapter interfaces;
- remove duplicate state projections and redundant validation;
- retain distinct workflow-specific execution, qualification and recovery rules.

### Phase 3 — Organize and clean

After module ownership is stable:
- remove unreachable, duplicate and obsolete code;
- consolidate package names and directory layout around bounded contexts;
- update imports, CLI, configuration and documentation;
- finalize installation, local run, server deployment and operator runbooks.

## 5. Safety and current authorization boundary

This plan does not grant additional live permissions.

The existing supervised Regular pilot remains limited to its already approved single-Dataset/single-batch scope. The existing unresolved model-attempt and Simulation evidence must be reconciled from original records before continuation. No new Provider call, Simulation POST, replay, refund, Alpha submission, Alpha mutation, live Super or live Optimization is authorized by this plan.

Offline development and tests may proceed, provided they do not mutate active pilot evidence, widen execution authority, or create an alternative route to external side effects.

## 6. Definition of completion

Phase 1 is complete only when all three workflow traces reach their workflow-specific terminal decision with durable lineage and tested recovery semantics. A terminal decision may be Candidate, rejected, stopped, or explicitly awaiting reconciliation; it must not be falsely reported as successful qualification.

Phase 2 is complete when each shared responsibility has one canonical owner and all three workflows still pass their end-to-end acceptance traces.

Phase 3 is complete when the resulting codebase is coherent, redundant code is removed, operator instructions are reproducible, and tests/CI pass on the exact final commit.

## 7. Execution evidence and iterative review

The first implementation batch adds a per-Task append-only JSONL event chain under `state/logs/execution_events/`. The event chain is SHA-256 linked, serialized by a single-writer lock and fsynced after each append. It captures Task lifecycle transitions and workflow-specific milestones. Existing ModelCallJournal, Campaign state, Super run records, Optimization source/branch/report records, Simulation Intent, Readback, Candidate/Preferred and Recovery receipts remain the authoritative detailed evidence.

Use `orchestration-execution-trace <TASK_ID>` to verify and inspect the complete local evidence chain. A missing journal is `TRACE_INCOMPLETE`, not success; corrupt history fails closed. See [EXECUTION_TRACE.md](../docs/EXECUTION_TRACE.md).

Optimization is performed in complete batches: first connect and instrument all three existing workflow paths, then review architecture and code together, then execute the consolidated offline regression suite. A discovered issue is recorded and addressed as part of the batch unless it creates an immediate safety hazard or makes further changes unsafe. No live external side effects are used to validate this instrumentation.
