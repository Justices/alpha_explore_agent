# Research State V1.1: Campaign, Budget, and Candidate Branches

Status: implementation design for [Scope V1.1](RESEARCH_SCOPE.md). This
document defines persisted state and transition rules; it does not claim the
current CLI implements them. The root [agent contract](../AGENTS.md) controls
research behavior and the [README](../README.md) describes current behavior.
The confirmed decisions in the `AI Agent框架分析` conversation also inform
this design: Workflow Scope, Research Scope, Research Profile and Execution
Policy are separate layers. An omitted Dataset permits evidenced Agent
selection; omitted Category, Delay and Universe require coverage of all
platform-valid combinations within the configured execution budget.

## Identity and storage

Store runtime records under the configured state root, outside the source
repository. Use stable opaque IDs for campaigns, Dataset subtasks, scope
combinations, and optimization branches. Every record has `schema_version`,
`created_at`, and `updated_at`. A receipt or evidence reference points to a
case-local artifact; a summary does not replace the artifact. Persist state
atomically and retain the existing single-writer and simulation-recovery
protections.

The current `campaigns/<case_key>.json` (schema 1) records a single current
Dataset and batch. Treat it as a legacy record. Do not reinterpret its
`current_dataset` as a complete multi-Dataset Campaign or overwrite it with
schema 2. A migration must preserve the original file and create a new record
with an explicit legacy origin and unknown coverage where evidence is absent.

## Campaign record

A Campaign represents one Regular Research request. Its scope is immutable
after work starts unless an authorized scope change creates a new revision
with a recorded reason. The record should contain:

```json
{
  "schema_version": 2,
  "campaign_id": "opaque-id",
  "workflow": "regular",
  "request": {
    "objective": "economic question",
    "region": "REGION",
    "dataset_ids": null,
    "category": null,
    "delay": null,
    "universe": null,
    "profile": "regular"
  },
  "scope_revision": 1,
  "budget": {"limits": {"batches": 8}, "reserved": {"batches": 0}, "used": {"batches": 0}},
  "dataset_subtask_ids": [],
  "status": "planning",
  "stop_reason": null,
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601"
}
```

`null` in the request means omitted by the user, not unrestricted platform
support. A supplied value or list is a hard constraint. Record the original
request separately from resolved platform combinations and record each
constraint's source (`user_specified`, `system_fixed`, or `agent_selected`).
`dataset_ids` may
contain one or several IDs; omission permits documented agent selection.
`profile` is `regular` or explicitly requested `ppa`. No PPA restriction
applies to the regular profile. Budget limits and stop policies belong to
Execution Policy, never to Research Scope. The resolved scope is immutable
for a batch and records its platform evidence and selection rationale.

## Dataset subtask and combination records

Each Dataset has one independent subtask, even when several share a Campaign.
Its record contains `campaign_id`, `dataset_id`, selection source and
rationale, catalog/detail evidence references and observation time, admission
outcome, ordered `combination_ids`, own batch and candidate references,
status, stop reason, and coverage counts. A subtask cannot silently switch its
Dataset ID.
Catalog enumeration alone is not an Agent selection decision: when Dataset
IDs are omitted, the Agent must select and justify its Campaign Dataset set
from the live eligible pool before execution.

Each combination records the exact Category, Delay, Universe and other
effective settings, platform support evidence, planned order, status, batch
references and outcome. The discovery snapshot must enumerate all
platform-valid combinations matching the user's constraints. If discovery is
incomplete, mark coverage `unknown`; do not assert an empty eligible set.
If a later catalog differs, preserve the original snapshot and create a
revision rather than rewriting completed work.

Combination statuses are `eligible`, `running`, `completed`, `blocked`,
`skipped_budget`, `stopped`, or `unresolved`. A completed combination means
its planned research was evaluated under recorded limits; it does not imply a
Candidate. Dataset and Campaign summaries derive from child records and
retain per-child failures, pending work, and unexamined combinations.

## Budget ledger and stopping

Set a Campaign limit before starting new work. The initial supported hard
limit can remain the existing explicit maximum batch count. Reserve one unit
before a non-idempotent batch intent, then reconcile the reservation from its
receipt; recovery must not charge or submit the same intent twice. Track
`limit`, `reserved`, `used`, and `remaining` for each budget dimension.
Record allocation by Dataset and combination so a global limit does not hide
which work was left undone.

Additional time, cost, concurrency and per-subtask limits require explicit
configuration and metering before they become enforced limits. Do not invent
defaults or treat unknown token/cost measurements as zero. At a limit, stop
starting work, preserve in-flight recovery data, and label remaining work
`skipped_budget`. User stop, blocked evidence, configured goal met and
evidence-backed exhaustion are distinct reasons. A budget stop never changes
the requested scope or establishes exhaustion.

## Optimization source and candidate branches

Optimization starts with an Alpha ID. A read-only source snapshot records
source type (`regular` or `super`), Region, applicable settings, exact
platform evidence and observation time. An inaccessible or ambiguous source
blocks branch creation. The source snapshot is immutable and identified by a
content hash or stable artifact reference.

Each proposed candidate branch contains:

```json
{
  "schema_version": 1,
  "branch_id": "opaque-id",
  "workflow": "optimization",
  "source_alpha_id": "ALPHA_ID",
  "source_snapshot_ref": "case-local-reference",
  "source_type": "regular",
  "objective": "improvement question",
  "constraints": {},
  "rationale": "hypothesis and failure criterion",
  "changes": [],
  "effective_settings": {},
  "prereg_ref": null,
  "simulation_intent_ref": null,
  "receipt_ref": null,
  "audit_ref": null,
  "status": "draft"
}
```

Branches are independent: one branch's expression, settings, evidence,
simulation, and audit cannot overwrite another's. The original Alpha and its
properties are never changed by the workflow. A branch may become a Candidate
only after the complete applicable audit and read-back. Branch comparison
must report both improvements and regressions against the source without
presenting a simulated result as a submission.

## Super Research state

Super Research uses its own top-level run and candidate lineage, with Region,
objective, budget, BRAIN mechanism identifier, mechanism evidence, effective
inputs, receipts and applicable checks. Its eligible Dataset/input scope is
derived from BRAIN's dedicated mechanism, not from Regular Dataset subtasks.
Current BRAIN documentation describes a SuperAlpha as a selection expression
over the account's submitted ACTIVE Alphas plus a combo expression. Selection
is constrained by Super settings including Region, Delay, selection handling
and selection limit. The platform simulation interface accepts type `SUPER`,
`selection`, `combo` and Super-specific settings. Thus the source pool is
Alpha-based and must be read from the platform; Regular Dataset catalog
discovery does not define it. The concrete validation and recovery path still
requires implementation. Do not create a fake Super record by reusing a
Regular expression or Dataset schema.

Platform references: BRAIN documentation pages `superalpha-overview`,
`selection-expression`, and `superalpha-results` (read 2026-11-09).

## Transition and reporting invariants

- Validate the request and live-supported scope before fixing settings or
  reserving a simulation budget.
- Record hypotheses, checks and failure criteria before simulation. Persist
  the intent before the platform call; reconcile an uncertain intent rather
  than reposting it.
- Keep `draft`, `simulated`, `pending`, `failed`, and `candidate` distinct.
  Submitted and active states require independent platform evidence.
- A Campaign's completion report lists requested, eligible, completed,
  blocked, unresolved and budget-skipped scope separately.
- Use the existing configured gates and current agent contract. This schema
  introduces no new threshold, automatic submission or property mutation.

## Implementation order

1. Add read/write validation for versioned Campaign, subtask, combination,
   budget, source and branch records without changing existing schema 1 reads.
2. Add Regular scope discovery and Campaign scheduling with one Dataset
   subtask per Dataset, preserving current serial execution and recovery.
3. Add Alpha-ID source lookup and independent optimization branch evaluation.
4. Inspect and integrate the BRAIN-native Super mechanism, then define its
   concrete input validation and audit.
5. Expose coverage and stop reasons in CLI status and final reports; migrate
   legacy records only with explicit, evidence-preserving conversion.

The precise platform Super interface, optional budget defaults, optimization
action set and legacy migration tooling remain open implementation decisions.


## Implemented runtime behavior (feature branch)

The current V1.1 feature branch persists Campaign, Dataset Subtask and Combination records with stable IDs, evidence references and coverage counts. Super runs are represented in the shared record store and retain platform-native settings/pool evidence, simulation intent, readback and pending-audit references. Optimization source snapshots and candidate branches retain immutable source lineage and property fingerprints; a post-simulation source read verifies that the source definition (identity, family, settings and expression definition) has not changed. Each completed branch emits an optimization report.

`recover-research <intent-path>` reconciles an interrupted Super or Optimization intent through the existing platform `resume_simulation_batch` command. The runtime validates its saved request/admission/receipt hashes and resumes the platform runner; it does not issue a new `simulate_super` or `simulate_batch` call. An unknown result without a valid platform recovery location remains unresolved. This command is exposed through the main CLI and the `status` command lists Super runs, Optimization sources and candidate branches.

This implementation status is specific to the feature branch and has not been validated against a live BRAIN account in this pass. Candidate audit remains pending unless the full applicable family audit has actually been completed. Automated tests were deferred at the user's request; run the repository checks before merging.


## Candidate and Preferred are separate states

Candidate is awarded only by the existing Candidate Qualification gate:
the existing Sharpe precondition, complete readable RA checks with zero
failures, production/self correlation limits, pairwise de-duplication and
all required evidence. Preferred is a second-stage selection and can only
evaluate a record whose final Candidate status is exactly `CANDIDATE`.

The `preferred-v1` policy currently requires Sharpe >= 1.25, Fitness >= 1.0,
Turnover in [0.01, 0.70], maximum weight <= 0.10, and a PASS result for
`LOW_SUB_UNIVERSE_SHARPE`. Missing measurements or unsettled checks produce
`PENDING_PREFERRED`; failed criteria produce `NOT_PREFERRED`. A non-Candidate
is `NOT_CANDIDATE`. Preferred never changes Candidate status or submits an
Alpha. Drawdown and Coverage are recorded as informational metrics until
explicit thresholds are approved.
