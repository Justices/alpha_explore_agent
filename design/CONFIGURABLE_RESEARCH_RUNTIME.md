# Codex Research Lead / AGY Executor — Configurable Runtime P0

Status: feature-branch implementation; not live BRAIN research authorization.

The active project boundaries are AGENTS.md and design/RESEARCH_SCOPE.md.
This design does not modify existing Regular, Super, Optimization, PPA,
CandidateQualificationService or simulation recovery policies.

## Boundary and adapters

A research task maps to a logical role; a role maps to a backend.
The RoleRoutedModelClient preserves the complete_json(task, context)
interface and the previous build_model_client factory. Legacy model router
configurations remain supported.

- research_lead: Codex CLI, read-only research planning and reflection.
- research_executor: official Antigravity SDK with a Gemini developer API key,
  prepares an authorized experiment execution order and attributes returned
  results. It has no direct BRAIN credentials or tools in P0.
- mock: deterministic fixture-backed model for offline cycle tests.
- OfflineSimulationGateway: synthetic results only; never calls BRAIN.
- Existing production BRAIN Gateway: remains outside P0.
- DecisionRecorder: records selected backend and agent_role in persisted logs.

Role selection is configurable via model.default_role, model.task_roles,
model.roles and model.backends. See config.example.json. No model SDK key is
written to repository config. Configure GEMINI_API_KEY in your environment.

## SDK security

An AGY instruction is not a security boundary. The SDK adapter explicitly
limits capabilities to FINISH, disables subagents, registers no custom tools,
MCP servers or skills, and denies all other tool calls. That is separate
from BRAIN's existing simulation-intent, authorization and recovery gates.

The orchestrator validates the execution plan against the complete list of
Codex-authorized experiment IDs before invoking the offline gateway. Any
unknown or missing ID blocks all execution.

The old agy_cli provider still exists for legacy, explicitly initiated work,
but is intentionally rejected inside new unattended role-router definitions.
No consumer Google account is silently scripted as a model backend.

To install the optional official SDK dependency:
    pip install -e ".[agy]"

## P0 mock smoke command

The following is a MANUAL test command, not a production run:

    python -m alpha_research.research_cycle \
      --config config.mock.example.json \
      --fixtures tests/fixtures/research_cycle_simulations.json \
      --research-id R1 --objective "test reversal" --region CHN

The command refuses all non-mock model backends and uses only an explicit
offline Simulation fixture. It persists DecisionRecorder calls and a synthetic
final report under the state root. To select a custom state root, supply
--state-root. This does not call the BRAIN API or perform Alpha submission.

The research action CANDIDATE is only a *proposal* here. The qualification
state is always NOT_EVALUATED; only the existing project qualification service
may determine an actual Candidate after complete authoritative evidence.

## Required verification before production enablement

1. Static compilation and tests/test_configurable_agents.py.
2. Legacy model client, recorder and model CLI tests.
3. Full existing offline orchestration and research workflow tests.
4. A separate manually authorized AGY SDK smoke test with a developer API key.
5. A separate authorized Codex CLI read-only smoke test.
6. Live Simulation gateway integration only after explicit operation policy,
   intent receipt persistence, recovery reconciliation and approval.
7. Never infer a platform success from mock scores or model text.

P0 does not claim crash-resilient multi-round learning, task queue support,
production tool execution, RA/PC/SC qualification or live API verification.


## P1 durable mock research

The durable loop is a separate, opt-in CLI mode:

    python -m alpha_research.research_cycle \
      --config config.mock.example.json \
      --fixtures tests/fixtures/research_cycle_simulations.json \
      --state-root /tmp/alpha-p1-state \
      --research-id R1 --objective "test reversal" --region CHN \
      --durable --max-rounds 1 --max-total-experiments 4 --max-per-round 4

DurableOfflineResearchRunner persists an immutable request, bounded research
rounds, model response checkpoints, exact experiment intents and results.
It uses a global SingleWriterLock and only accepts the concrete
OfflineSimulationGateway; it is not an authorized live execution interface.
The same completed task returns its persisted report without restarting it.

New model calls may use arrays of mock responses, selected by round_index.
Future rounds receive previous round reports and reject identical experiment
expression+settings signatures. This enables deterministic offline
multi-round optimization tests and prevents resetting the experiment budget.

An interrupted experiment is an unresolved intent. Even a mock experiment is
not automatically repeated. Explicit offline fixture reconciliation is
available as reconcile_offline_fixture(research_id, experiment_id), followed by
run(..., resume_blocked=True). Never use fixture reconciliation to recover a
live simulation. Real simulations require the existing BRAIN recovery service.

All persisted mock states remain NOT_EVALUATED for Candidate qualification.


## State / provider identity hardening

The durable request pins an SHA-256 of the effective role/backend routing.
Resuming the same research ID with changed routes, timeouts, mock fixtures,
model definitions or provider config is rejected as an immutable request
change. Start a new research ID or build a separately audited migration;
never silently alter a partially executed study.

On a BLOCKED round, invalidate_model_checkpoint(research_id, task, reason)
explicitly removes only the named erroneous model response checkpoint,
retaining the old context/response hashes and a reason in the run record.
It does not clear or replay a simulation intent. Plans cannot be invalidated
after simulation intents exist. Operator action is required for the subsequent
resume_blocked=True call. An experimental source remains NOT_EVALUATED.
