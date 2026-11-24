# Supervised local REAL Regular end-to-end pilot (not P7 production release)

This entrypoint connects the **existing live Regular domain executor** to the
same `MainOrchestrator` Task, model identity, budget, checkpoints and recovery
callbacks. It is a bounded, explicit opt-in LOCAL acceptance pilot, not the
independently certified production authorization path. It does not remove any
`P7_PRODUCTION_BLOCKED` release blockers.

## Preconditions

- Use the same non-mock `--state-root`, repository branch and model registry
  throughout. This is a **real** BRAIN and **paid real model** workflow.
- A locally configured `config/nastu_quant.json` must declare a finite
  `model_governance.max_calls` and `max_reported_tokens`, plus `failover=manual`.
- Configure the model credentials via the registered backend environment
  variables and BRAIN credentials via `BRAIN_CREDENTIALS_FILE`. Do not print
  or commit credentials.
- Both `--lead` and `--executor` must specify matching registered, non-mock
  roles in **both** commands.
- Choose a **new**, never-executed Task and a **new objective**. This pilot
  refuses existing campaigns to avoid silently resubmitting previous work.
- The pilot refuses all multi-Dataset, unpinned Universe, non-Delay-1,
  multi-batch, stopped and mock workspaces.

## Stage 1 — prepare immutable Task and exact model identity (no external calls)

```powershell
python -m alpha_research `
  --state-root "D:\quant\alpha_explore_agent\alpha-research-live" `
  --lead deepseek --executor deepseek `
  orchestration-prepare `
  --workflow regular `
  --objective "USA analyst10 one-batch supervised pilot 2026-11-24" `
  --region USA --delay 1 --universe TOP3000 --dataset analyst10
```

Copy the **new** `task_id` from `PREPARED_NOT_AUTHORIZED`. The `deepseek`
backend name is an example: use your registered ID(s). This step is not a
Simulation approval and makes no BRAIN or model request.

## Stage 2 — explicit, one-time local live pilot

**This command intentionally makes real metered external calls.** Review
provider pricing and BRAIN experiment quotas before using it:

```powershell
python -m alpha_research `
  --state-root "D:\quant\alpha_explore_agent\alpha-research-live" `
  --lead deepseek --executor deepseek `
  orchestration-local-pilot-regular "<NEW_TASK_ID>" `
  --allow-model-call `
  --allow-brain-simulation
```

Both opt-ins are required; without either, it refuses before model setup or
BRAIN authentication. Model calls are bounded by the configured real model
ledger, **not** by a single-model-call promise. The gateway can dispatch at
most **one** `simulate_batch` command, which contains 1–8 expressions and
corresponds to at most one `POST /simulations`. The domain also performs
Catalog/field/operator reads, persistent preregistration, native result
readback, RA/correlation/Candidate/Preferred qualification and records all
unresolved evidence. Diagnostic simulations, Super simulations, auto tagging,
automatic submission and other Alpha mutations are denied at this pilot's
gateway boundary. BRAIN authentication itself may call `POST /authentication`.

The same live CLI Gateway and model are injected into the domain rather than
spawning a second runtime. The persistent Task receives the domain Research ID,
Simulation Intent/Recovery target, execution checkpoints, candidate projection,
and budget consumption. Completion means **the bounded workflow reached its
end and its evidence is recorded**, not that an Alpha passed Candidate.

## Stage 3 — read the Task and handle unresolved outcomes

```powershell
python -m alpha_research `
  --state-root "D:\quant\alpha_explore_agent\alpha-research-live" `
  orchestration-status "<NEW_TASK_ID>"
```

Expected local pilot states:

- `COMPLETED`: validate/execute/qualify/collect all DONE, one consumed Task
  batch, non-synthetic evidence; inspect Candidate and Preferred statuses.
- `BLOCKED` during validate: no Simulation was attempted, the precise Task
  or configuration scope needs correction in a **new** Task.
- `BLOCKED` during execute: the real provider response may be unknown; the
  Task retains reserved budget, original Research ID and exact recovery target.
  **Do not re-run the same Task or repost the Simulation**. Use existing
  domain `recover-task`/receipt reconciliation after verifying BRAIN
  readback and original intent, not a fresh submit.
- `BLOCKED` during qualify/collect: do not redo execute; inspect the
  persisted result and unresolved deterministic audit.

## What this does NOT prove

- It is a controlled local executable test of the real domain path, **not**
  a globally trusted P7 production grant. The independent PKI/Authority,
  official Provider billing/readback, managed Stop propagation, durable
  independent one-shot write authorization and production operator approvals
  remain outstanding.
- The first pass supports Regular only. Super and Optimization need their
  own measured pilot constraints and governed BRAIN operation types.
- No automatic Alpha submission, property mutation or P7 release occurs.
- Passing offline mock/CI remains different from an actual user-run pilot.
