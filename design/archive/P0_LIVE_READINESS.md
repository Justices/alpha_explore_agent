> **历史档案**：原有设计/验收描述不再是当前真相来源。详见 [整体设计](../CURRENT_DESIGN.md)。

# P0 — Controlled live research readiness

This is an operator runbook for the **existing** Regular/Super/Optimization
BRAIN workflows, not a claim that live providers have already been verified.
The current MainOrchestrator still uses a mock-only adapter. There is no
Alpha submission capability. Existing Regular workflows **can apply and verify
research provenance tags after readback**; approval for live execution must
therefore also account for those platform tag writes. Other Alpha property
mutations are outside this rollout.

## Observed model-only acceptance evidence (operator report)

On 2026-11-20, an operator reported a Windows `models smoke --role both
--allow-model-call` result of `PASS` for the DeepSeek registration selected
as both Research Lead and Research Executor. Each role completed one
`model_probe_v1` call; the reported usage was 345 and 349 tokens
respectively, and `brain_invoked=false`. This report demonstrates the
model-only acceptance path **on that operator's environment**. The underlying
local audit files and platform connectivity have not been independently
verified in the repository or offline CI. It does not authorize Simulation,
Alpha submission, or tag/property mutation.

For the consolidated status and document index, see
[Current Design](../CURRENT_DESIGN.md).

## Implemented locally (no network)

1. The CLI `plan-regular`, `plan-super`, `plan-optimization`, BRAIN evidence
   readback and non-Regular recovery no longer require an unused model client
   or role registry. They still open the BRAIN gateway when invoked.
2. Real research routes require both `--lead` and `--executor`, selected from
   the registered Agent/LLM definitions. Fixture/mock model backends are
   rejected for live BRAIN research, but remain available for offline tests.
   Registered live routes require `model_governance` with positive attempt
   and reported-token limits and `failover=manual`.
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

An explicitly authorized model-only check can run the two roles
**sequentially**. The first contract mismatch aborts the second request.
No Simulation or BRAIN credentials are involved.

```sh
# These commands require model credential variables (e.g. ZAI_API_KEY and
# MOONSHOT_API_KEY) on the SAME machine executing the Python process.
python -m alpha_research --state-root "<state-root>" \
  --lead glm --executor kimi models doctor

# First check the routing and contract shape WITHOUT any model call.
python -m alpha_research --state-root "<state-root>" \
  --lead glm --executor kimi models smoke --role both

# This final command explicitly authorizes at most ONE request per role:
python -m alpha_research --state-root "<state-root>" \
  --lead glm --executor kimi models smoke --role both --allow-model-call
```

The two results each contain a unique research ID, response digest, declared
backend and reported usage. Audit evidence stays under
`<state-root>/state/logs/model_calls/` and model budget under
`<state-root>/state/logs/model_budget/`. No raw response or key should be
copied into a ticket. An actual dual-role `PASS` is the acceptance criterion;
`NOT_EXECUTED` only proves dry-run setup. If the first response is not the
exact `model_probe_v1` object, the second call is not attempted. The first
request is bounded to 512 generated tokens using the official
OpenAI Python SDK (`max_retries=0`); reported-token governance alone does
not impose a hard currency limit, and charges remain subject to provider
accounting. The preset GLM backend uses the mainland BigModel
`https://open.bigmodel.cn/api/paas/v4` API root. Use a BigModel-issued
key in `ZAI_API_KEY`; the international Z.ai endpoint has a distinct
credential authority.

The bundled model registry uses GLM-4.6 for `glm` and currently maps `kimi`
to `kimi-k2.5`. The current official Kimi parameter documentation
highlights K2.6, K2.7 and K3 families, **not a guarantee that K2.5 remains
callable**. Verify the active model ID in the authenticated provider's model
catalog before an authorized probe; make deliberate registry changes rather
than silently switching the model during a running research ID.

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
