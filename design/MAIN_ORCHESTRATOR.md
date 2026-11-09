# Main Orchestrator — offline-first P1–P4

The control plane lives in `src/nastu_quant/research/orchestrator.py`. It does not create a BRAIN gateway, call live APIs, run a Simulation, or submit an Alpha.

## P1: task contract and state machine

Supported workflows: `regular`, `super`, `optimization`. Each task has an immutable request digest, independent task ID, bounded experiment budget, durable step checkpoints, and status `CREATED / RUNNING / COMPLETED / BLOCKED`. A completed task is **not** a qualified Candidate; qualification is a separate step/result.

The orchestration steps are `validate → execute → qualify → collect`. Step outputs are JSON objects stored under `state/research_v1_1/orchestration/<task-id>.json`.

## P2: crash/fault handling

Before calling an adapter, the orchestrator writes a `RUNNING` step and reserves experiment budget. If the adapter raises, the task is blocked. Re-entry never automatically replays a blocked step; an independently verified receipt may be recorded with `reconcile` and processing can then resume from the next step. This is intentionally conservative: it cannot infer that a crashed external Simulation failed.

Single-writer lock is mandatory for CLI mutations; direct library users must hold the same lock. The orchestrator does not yet implement distributed locking or automatic reconciliation of platform receipts.

## P3: Candidate Pool

The `pool_snapshot` function builds a policy-aware, read-only view of the existing global Candidate Pool. The `collect` adapter step carries qualification output but cannot promote a Candidate by itself. The existing `CandidateQualificationService` remains the sole admission authority.

## P4: mock integration

`OfflineResearchAdapter` accepts fixture keys such as `regular.validate`, `regular.execute`, `regular.qualify`, `regular.collect`. It cannot call a live gateway. Unit tests cover all three workflows, checkpoint durability, unknown execute outcome, manual reconciliation, no replay, missing fixtures, and request tampering.

Example:

```bash
python -m nastu_quant --state-root /tmp/alpha-state orchestrate-offline \
  --workflow regular --objective "mock-only research" --fixtures fixtures.json
python -m nastu_quant --state-root /tmp/alpha-state orchestration-status <task-id>
```

## Existing workflow entrypoint adapter (mock-only)

`src/nastu_quant/research/workflow_adapter.py` connects the orchestrator's
`execute` step to the existing `research.entrypoints.dispatch` routes:
`run-campaign` (Regular, one batch), `run-super` (Super), and
`run-optimization` (Optimization with mandatory source Alpha ID).
The adapter requires a gateway with an explicit `is_offline_mock` marker
and a caller-injected agent. It does not construct a real Gateway or model.
For unit tests, the dispatcher itself is injected, so no BRAIN API or
Simulation endpoint is invoked.

The existing executors own CandidateQualificationService. The orchestration
`qualify` step only verifies their returned evidence, and the `collect`
step builds a policy-aware, read-only Candidate Pool snapshot. This avoids
double RA/PC/SC checks and accidental promotion.

This is an **entrypoint contract integration**, not a complete simulated
end-to-end exercise of the three existing executors. Those executors require
richer mock gateway/model responses and have their own test suites.

## Remaining production integration boundary

The current adapter is a **fixture-only contract**, not a production connection to the existing Regular Campaign, native Super, or Optimization executors. Before enabling a live adapter, implement an explicit operation policy, reconcile saved Simulation intents/receipts with the existing recovery API, validate budget accounting against the actual runner, and run a separate approval-controlled test. Automatic Alpha submission is out of scope. No live permission is implied by successful offline tests.
