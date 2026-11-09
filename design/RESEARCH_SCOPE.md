# Research Scope V1.1 and Product Boundaries

Status: confirmed product scope; design target, not a statement that all workflows
are implemented. This document records the decisions for V1.1. It does not
change runtime behavior, configuration, thresholds, or existing formal rules.
The repository's [agent contract](../AGENTS.md) governs agent behavior; the
[README](../README.md) describes the current implementation. The earlier
[requirements](AGENT_REQUIREMENTS.md), [design](AGENT_DESIGN.md), and
[knowledge design](AGENT_KNOWLEDGE_DESIGN.md) include historical assumptions;
their status notes and the agent contract must be read before using them.

## 1. Product purpose and workflows

The product researches BRAIN Alphas within an explicit user scope, preserving
evidence and presenting auditable results. V1.1 has three workflows:

1. **Regular Alpha Research:** investigate one or more Datasets in a Region;
   optionally apply a PPA Profile.
2. **BRAIN-native Super Alpha Research:** research Super Alphas using BRAIN's
   dedicated mechanism and its platform-defined eligible input scope.
3. **Alpha Optimization:** investigate independent candidate branches derived
   from a specified existing Regular or Super Alpha.

A workflow is research and evaluation, not a guarantee of a valid, eligible,
submittable, submitted, or active Alpha. The current contract prohibits
automatic Alpha submission and Alpha property mutation.

## 2. Inputs and scope resolution

| Workflow | Required input | Optional inputs and resolution |
| --- | --- | --- |
| Regular Research | Region | Dataset ID(s), Category, Delay, Universe, objective, PPA Profile, budgets and other permitted settings. Dataset may be user-specified or agent-selected. |
| Super Research | Region | Objective, budgets and other inputs supported by BRAIN's Super mechanism. Eligible Dataset/input scope comes entirely from that mechanism. |
| Optimization | Alpha ID | Objective, budgets and compatible constraints. Read the original Alpha's type and settings from platform evidence; support both Regular and Super. |

Region is mandatory for research campaigns. For Optimization the Alpha ID is
mandatory; resolve and record the source Region from the Alpha and reject any
conflicting user-supplied Region. Do not infer missing source facts.

User-specified scope values are hard constraints. Validate them against current
platform evidence and each other; an invalid, unavailable, inaccessible or
conflicting value must be reported, never silently replaced or broadened.
Changing an established scope requires the approval required by the agent
contract. Other effective settings must be supported by evidence and fixed
before each backtest.

For Regular Research, omitted Category, Delay or Universe means the **full set
of platform-valid combinations** consistent with the Region, Dataset and other
user constraints. This is a coverage obligation, not permission to invent a
Cartesian product: validate combinations against current platform evidence.
Plan and record the eligible set, the order of work, and any unavailable
evidence. Configured budgets may limit what is actually explored; report the
unexplored or unresolved remainder and do not call a budget-limited run
exhaustive. An omitted Dataset permits agent selection from eligible Datasets,
with recorded evidence and rationale; it does not authorize replacing a
user-specified Dataset. A fixed Dataset with no valid combination is reported
as such. No valid Dataset or inability to establish eligibility is a blocked or
unresolved result, not a reason to relax constraints.

## 3. Regular Research and Campaign boundary

A Campaign can cover multiple user-specified or agent-selected Datasets. Each
Dataset is an independent subtask with its own identity, admission evidence,
scope combinations, hypotheses, batches, status, results and failure history.
A subtask may contain multiple valid Category/Delay/Universe combinations;
each backtest has one fixed effective combination. Record parent Campaign and
Dataset lineage. Aggregate progress without merging or misattributing evidence
or claiming that one Dataset's result represents another. A failure or stop in
one subtask must be visible independently; Campaign-level limits and stops
still apply. Scheduling and concurrency remain subject to existing approved
rules; multi-Dataset scope does not itself authorize parallel execution.

Each Regular expression remains within one Dataset and validated field and
operator constraints, as required by the agent contract. Dataset admission,
field semantics, settings and applicable checks require current evidence.
Model hypotheses and diagnostics are not evidence of Alpha performance.

### Optional PPA Profile

PPA is an explicitly requested Profile inside Regular Research, not a separate
workflow or a default restriction on ordinary Regular research. Apply the
project's current PPA rules only when requested; preserve the distinction
between local PPA qualification, complete Regular Candidate eligibility and
platform-assigned classification. A local result does not establish platform
classification or substitute for the full applicable Candidate audit.
Missing evidence remains pending. Platform rules and the project contract may
change; do not derive a new threshold from this scope document.

## 4. BRAIN-native Super Alpha Research

Use BRAIN's dedicated Super Alpha mechanism, including its platform-defined
construction, eligible input/Dataset scope, settings and checks. Region is
required. Do not treat Super as a Regular expression, apply Regular Dataset
selection or one-Dataset assumptions to it, or invent a replacement mechanism.
Validate user constraints against the actual Super mechanism; unsupported or
unverifiable constraints remain blocked or unresolved. Capture the mechanism,
platform evidence, effective inputs, candidate lineage, simulation results
and applicable checks. The exact platform interface and eligibility rules
require discovery and validation; this scope does not assert their details.

## 5. Alpha Optimization

Resolve the required Alpha ID and confirm whether the source is Regular or
Super before planning. Capture a read-only source snapshot, effective settings,
available diagnostics and evidence; report inaccessible or ambiguous sources.
Respect the user's objective and constraints and the rules applicable to the
source type. A proposed modification is an independent candidate branch with
its own identity, source ID and snapshot reference, rationale, changed elements,
settings, pre-simulation evidence, results and audit. The original Alpha and
its properties remain unchanged. Each branch is evaluated on its own evidence;
a simulated branch is not automatically a Candidate or submission.

This workflow is broader than the existing evidence-linked remedy process.
It does not override that process's approved restrictions or authorize
unbounded parameter sweeps, changing locked scope to improve scores, or
automatic mutation of the source. How to reconcile specific optimization
actions and branch limits with existing requirements is an implementation
decision requiring review.

## 6. Outputs and evidence

Each run should produce a reviewable record containing:

- request, workflow, constraints, objective, budgets, effective settings and
  their provenance; platform evidence and observation time;
- Campaign/subtask or source/branch lineage, selection and scheduling rationale,
  eligible, completed, skipped, blocked and unexamined scope;
- hypotheses and pre-backtest evidence, simulation intent and receipts,
  recovery identifiers, results and applicable check outcomes;
- separate states for draft, simulated, pending, failed, Candidate and, only
  when independently evidenced, submitted or active; PPA qualification and
  platform classification are separate fields when applicable;
- stop reason, budget consumption, failures, unknowns and actionable next steps.

Preserve case-local evidence and exact settings. Unreadable, missing, stale or
pending evidence is unresolved, not a pass. Existing Candidate gates,
correlation rules and user resolution of pairwise conflicts remain in force
where applicable. Never repost a possibly submitted simulation; recover and
reconcile it under the existing contract.

## 7. Budgets, stopping and completeness

Configure bounded resources before execution, including applicable simulation
or batch limits and any time, cost, concurrency or per-subtask limits. Record
the effective limits, allocation, consumption, scheduling decisions and
remaining work. A budget is an execution limit, not a change to the requested
scope. Stop starting new work when a limit is reached, a user stop is received,
a required rule or evidence blocks safe progress, or the configured goal is
met. Handle in-flight operations and recovery according to the existing
contract; a stop does not imply cancellation of a platform operation.

Distinguish completed coverage, budget-limited partial coverage, user-stopped,
blocked, pending and evidence-backed research exhaustion. Poor results,
technical failures, inaccessible platform information or a budget stop do not
by themselves establish exhaustion. Do not count pending or failed work as a
completed Candidate. Existing bounded-loop and target behavior is described
in the README; this document does not change it or establish new default
budget values or automatic stopping targets.

## 8. Dependencies and exclusions

Live BRAIN evidence governs available Regions, Datasets, Category/Delay/
Universe combinations, fields, operators, simulation settings, Super
mechanisms, account access, checks and platform classification. Retain the
evidence and its observation time; revalidate when needed. A platform response
or local aggregate flag does not replace applicable project gates. The model
must not invent platform facts or override deterministic failures.

Excluded: automatic Alpha submission, automatic Alpha property changes,
modification of the source Alpha, silently changing user constraints, using
another repository as a runtime dependency or policy source, and treating
diagnostic probes as performance evidence. No rule, numeric threshold, live
platform capability or implementation is changed by this document.

## 9. Open items and implementation status

- Define and validate the BRAIN-native Super interfaces, input eligibility,
  supported constraints, evidence and Super-specific audit.
- Design the Campaign and branch schemas, combination discovery, scheduling,
  isolation, recovery and coverage reporting; establish how catalog changes
  during a run are represented.
- Define budget dimensions, defaults, allocation and fairness across Datasets
  and combinations, and explicit goal/stop configuration.
- Specify Optimization actions, preservation constraints and branch limits
  for both types, and reconcile them with approved remedy and scope rules.
- Resolve any required updates to existing requirements, the agent contract,
  configuration, CLI and validation through their normal approval process.

The current README describes bounded Regular research and a single selected
Dataset per batch, not full V1.1 Campaign coverage, native Super research or
Alpha-ID optimization. In particular, current configuration and campaign
state are not a completed V1.1 schema. Historical design passages with fixed
scope, universal PPA-like limits or other superseded assumptions must not be
taken as current behavior. This document records product direction without
silently changing those files or the controlling contract.
