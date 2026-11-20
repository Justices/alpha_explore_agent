# P0 — Controlled live research readiness

This is an operator runbook for the **existing** Regular/Super/Optimization
BRAIN workflows, not a claim that live providers have already been verified.
The current MainOrchestrator still uses a mock-only adapter. No automatic
Alpha submission or Alpha property mutation is authorized by this runbook.

## Implemented locally (no network)

1. The CLI `plan-regular`, `plan-super`, `plan-optimization`, BRAIN evidence
   readback and non-Regular recovery no longer require an unused model client
   or role registry. They still open the BRAIN gateway when invoked.
2. Real research routes require both `--lead` and `--executor`, selected from
   the registered Agent/LLM definitions. Registered live routes require
   `model_governance` with positive attempt and reported-token limits and
   `failover=manual`.
3. The local Workbench lists registered backends through `GET /api/models`
   without exposing API keys. New and resumed Regular Campaigns require
   explicit Lead and Executor selections. Local readiness is checked before
   a worker process starts, and completed web launches persist a selected
   role profile digest for recovery consistency.
4. `preflight` checks local config, Region constraints, model registry,
   executable/API-key/SDK presence, model budget, and the external BRAIN
   credentials-file **shape**. It never logs in, probes a provider, runs
   Simulation or grants any permission.

## Prepare local state

From the repository checkout:

```sh
python -m pip install -e .
# Place config.example.json at <state-root>/config/nastu_quant.json,
# fill the real research scope and bounded model_governance policy.
# Set BRAIN_CREDENTIALS_FILE to an external JSON credential file.
# Export the chosen LLM key environment variables or authenticate the
# intended CLI agent under the service account.
python -m alpha_research --state-root "<state-root>" \
  --lead glm --executor kimi preflight --region CHN
python -m alpha_research --state-root "<state-root>" \
  --lead glm --executor kimi models doctor
python workbench/serve.py --state-root "<state-root>" --port 8766
```

`READY_LOCAL` proves **only local prerequisites**. It does not verify
actual BRAIN authentication, model API health, token balance, quota, model
JSON contract, dataset availability, safe execution permissions or test
quality. If a flag is `BLOCKED`, fix that prerequisite before live work.
Keep secrets outside the repo and never paste them into issue trackers.

## Explicitly authorized live acceptance stages (not run by offline CI)

### Stage A — Model-only probes

With explicit permission for paid model calls and appropriate credentials,
run each selected role separately:

```sh
python -m alpha_research --state-root "<state-root>" \
  --lead glm --executor kimi models smoke \
  --role research_lead --allow-model-call
python -m alpha_research --state-root "<state-root>" \
  --lead glm --executor kimi models smoke \
  --role research_executor --allow-model-call
```

Pass requires a valid `model_probe_v1` JSON contract and durable budget/audit
evidence for both roles. A local doctor alone is insufficient.

### Stage B — BRAIN read-only admission

With separately approved BRAIN read access, authenticate and run one
`plan-regular` for a specified Region and Dataset, for example:

```sh
python -m alpha_research --state-root "<state-root>" \
  plan-regular --region CHN --dataset "<verified-dataset-id>" --delay 1
```

Pass requires a verified account, real Dataset detail and exact-scope
Region/Delay/Universe, validated field and operator catalogs and a saved
non-simulation plan. This command must not issue a Simulation POST.

### Stage C — One controlled platform Simulation

**Do not run this without explicit authorization for BRAIN Simulation.**
Set one concrete Dataset, `--max-batches 1`, a new research ID and an
operator-approved cost/variant limit. The command enters the existing live
BRAIN POST code path:

```sh
python -m alpha_research --state-root "<state-root>" \
  --lead glm --executor kimi run-campaign \
  --region CHN --dataset "<verified-dataset-id>" --max-batches 1 \
  --objective "approved bounded hypothesis"
```

Pass requires pre-backtest receipts, pinned settings and input fields,
Simulation Intent/POST marker, a real platform receipt, Alpha readback,
applicable audit and a consistent durable result. Test partial-write failure,
recovery without a second POST, explicit stop and restart, and human review
of Candidate status. A single Campaign batch may contain more than one
simulation variant; verify the exact count before authorizing Stage C.
Automatic Alpha submission remains out of scope.

## Remaining production work

- The unified MainOrchestrator has a mock-only workflow adapter: production
  task orchestration, durable worker queue and restart supervision remain P1.
  CLI direct dispatch already reaches the existing production BRAIN runtime.
- Workbench is intended for one local operator. Shared access requires
  authenticated HTTPS, dedicated worker/queue and account isolation.
- Live provider behavior, quotas, BRAIN API compatibility and production
  recovery have **not** been established by offline tests.
- `preflight` is deliberately permission-neutral: no readiness result
  authorizes external spending or platform mutation.
