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
| `orchestration/engine.py` | Main workflow task state machine, budget reservations, verified reconciliation |
| `orchestration/offline_adapter.py` | Both explicitly offline adapters; never the live BRAIN gateway |
| `state/fingerprint.py` | Canonical JSON digest |
| `state/sealed.py` | One persisted state checksum contract for both offline research checkpoints and model-call ledgers |
| `integrations/model_prompts.py` | Canonical task instructions, field-evidence vocabulary and knowledge policy shared by CLI, LLM API and Antigravity SDK |
| `integrations/model.py` | Model transport and durable DecisionRecorder; consumes prompts without defining them |
| `agents/configuration.py` | Fail-closed, snapshot-isolated role/backend routing configuration |

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

Run `python -m unittest discover -s tests -v` without BRAIN credentials.
Never enable real Simulation or Alpha submission merely to validate a refactor.
