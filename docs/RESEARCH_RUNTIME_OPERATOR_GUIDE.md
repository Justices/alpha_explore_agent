# Research Runtime — Unified Operator Guide (P4.19–P4.24)

This is the **current operational entrypoint** for orchestrated Super Alpha
construction and Optimization. Earlier P4.x documents describe incremental
implementation history; use this guide for the current CLI and safety contract.

## One command to locate a blocked Task

```bash
python3 -m alpha_research \
  --lead deepseek --executor deepseek \
  orchestration-research-status \
  6095655c13d44733acbae021f1afd88e
```

Pass the **same** `--state-root` used to create that Task, if non-default.
This is a strictly read-only inspection and requires **no manually authored
Manifest**, approval files, network access, BRAIN account session or paid
model. It reports the pinned scope, Simulation budget, and a compact state
for each component:

| Component | Not started | Prepared | Integrity / recovery issue |
|---|---|---|---|
| Research coordinator | `AWAITING_RESEARCH_PREPARATION` | `RESEARCH_PREPARED_OFFLINE` | `RECONCILIATION_REQUIRED` |
| Model attempt | `NO_MODEL_ATTEMPT` | `MODEL_MOCK_COMPLETED` | `MODEL_CALL_MAY_HAVE_STARTED`, `MODEL_MOCK_UNKNOWN`, or `RECONCILIATION_REQUIRED` |
| Internal Manifest | `AWAITING_RESEARCH_OUTPUT` | `GENERATED_LOCAL_NOT_AUTHORIZED` | `RECONCILIATION_REQUIRED` |
| Local Grant audit | `NOT_RESERVED` | `RESERVED_RESEARCH_GRANT_AUDIT_ONLY` | `RECONCILIATION_REQUIRED` |
| Catalog contract | n/a | `CATALOG_PROTOCOL_VALIDATED_NO_LIVE_DRIVER` | `RECONCILIATION_REQUIRED` |
| Real research runtime | `RESEARCH_RUNTIME_LIVE_BLOCKED` | **Still blocked** | `RECONCILIATION_REQUIRED` |

If any component needs reconciliation, the overall status is
`RECONCILIATION_REQUIRED`. The command never silently clears, replaces,
replays or consumes any journal or receipt. A valid Manifest does not
authorize Simulation; it is an immutable snapshot of a validated candidate.

## P4.21 — A verified evidence chain, not just an existing file

The aggregate reports `AWAITING_INDEPENDENT_RESEARCH_AUTHORIZATION` **only**
when all three independent observations are consistent:

- Research coordinator: `RESEARCH_PREPARED_OFFLINE`
- Model attempt: `MODEL_MOCK_COMPLETED`
- Immutable Manifest: `GENERATED_LOCAL_NOT_AUTHORIZED`

A Manifest without a completed preparation journal is still
`RESEARCH_OUTPUT_NOT_READY_LIVE_BLOCKED`, with
`next_required=VERIFY_RESEARCH_PREPARATION_CHAIN`. A terminal preparation
without the required model/Manifest evidence, or an orphaned completed
model attempt, requires manual reconciliation. Do not replay an ambiguous
or already completed paid model call to repair an incomplete journal.

Every component verdict must match the current Task ID, schema and negative
permission assertion. Unknown states, mismatched identities and invalid
live-plan claims fail closed to `RECONCILIATION_REQUIRED`. These checks are
read-only and do not create additional authorization or issue new grants.

## P4.22–P4.24 — Durable terminal outcomes and canonical Task admission

The offline model reservation **and its finalization** now use the same
single-writer OS lock. Only a valid, sealed, pending, Task-matched,
policy-matched attempt can transition once to `MODEL_MOCK_COMPLETED` or
`MODEL_MOCK_UNKNOWN`. A second callback cannot overwrite an existing
terminal result. A symlinked writer lock or tampered policy fingerprint
blocks completion; ambiguous outcomes are not retried automatically.

Model actions are bound to the Task workflow: `construct_super` is valid
only for Super and `optimize_alpha` only for Optimization. The reservation
rechecks the Task after acquiring the lock; a Task that has already started
cannot gain a fresh model-call reservation.

Manifest publication, the research preparation coordinator and scripted
model reservation all use
`orchestration/research_task_contract.py:is_fresh_research_task`. It rejects
consumed steps, changed budget, persisted recovery evidence and incorrect
JSON numeric types. This is a shared *offline admission* predicate, not a
production grant. The simulation budget remains untouched.

## Run a complete local research rehearsal

Run **only in a new, dedicated test root**. Never substitute the state root
holding the real BRAIN read evidence or a production Task.

```bash
python3 -m alpha_research \
  --state-root /tmp/alpha-research-sandbox \
  --lead deepseek --executor deepseek \
  orchestration-prepare --workflow super \
  --objective "offline Super candidate rehearsal" \
  --region USA --delay 1 --universe TOP3000

python3 -m alpha_research \
  --state-root /tmp/alpha-research-sandbox \
  --lead deepseek --executor deepseek \
  orchestration-research-prepare <NEW_TEST_TASK_ID> --mode mock

python3 -m alpha_research \
  --state-root /tmp/alpha-research-sandbox \
  --lead deepseek --executor deepseek \
  orchestration-research-status <NEW_TEST_TASK_ID>
```

The rehearsal runs the actual Super/Optimization candidate-preparation
logic against **built-in scripted Gateway/Model** objects. It uses the
same Gateway- and driver-frame read policy, enforces an exact research
scope, journals the model attempt before returning a result, validates
expressions, auto-persists the Manifest and stops before Simulation Intent
or POST. Local `set_case` never becomes a platform request. The offline
root is permanently marked; a root with real platform evidence cannot
be converted into an offline mock.

The Simulation Task remains `CREATED`; `budget.used=0` and
`budget.reserved=0`. No paid model, BRAIN Simulation or Alpha submission
is invoked. An ambiguous or corrupted model attempt cannot be replayed.

## P4.19 driver, issuer and SDK boundaries

- **Gateway + Driver:** `research_catalog_scope` and
  `--research-catalog` are separately defined, with exact typed
  Region/Delay/Universe/EQUITY checks, bounded OS-stage Alpha pagination
  and source-Alpha binding for Optimization. **Startup is denied before
  credential read and platform-client creation** until an independent
  grant consumer exists. Raw stdin, forged grant fields and mutation
  requests cannot bypass admission.
- **Research grant consumer:** `research_global_consumer.py` creates a
  precise, non-secret handoff and checks a separately signed Ed25519
  consumption ACK. A user-supplied public-key file does **not** attest
  independent issuer custody or globally atomic grant consumption.
  The real network consumer does not exist; `consume_global_research_grant`
  explicitly raises.
- **SDK cost boundary:** `research_sdk_boundary.py` compiles a fixed
  OpenAI-compatible request shape with explicit output ceiling, one
  completion and no tools/streaming, and audits reported token usage.
  No live SDK transport is installed. The reported number of tokens, a
  claimed token count, and a signed tariff digest are **not hard USD
  controls or verified provider billing**.

## Production release prerequisites

The orchestrated live path remains **blocked** until all are implemented,
verified and separately approved:

1. Independently controlled remote Grant service with globally atomic,
   one-time consumption, durable receipt and revocation.
2. Driver-side session authentication and exact read-only Catalog
   enforcement, plus remote readback/usage reconciliation.
3. SDK-side maximum output enforcement, model-specific tokenization,
   independently verified USD pricing and hard global spend reservation.
4. Cross-process crash/failure evidence with uncertain outcomes requiring
   reconciliation and **no automatic paid retry**.
5. Explicitly scoped real canary authorization with observability and
   operator stop; Simulation POST and Alpha submission require *separate*
   approvals even after the read-only Agent is available.

Never infer production authorization from a locally verified Ed25519
signature, a successful offline test, a valid Manifest or a local Grant
reservation. The code intentionally returns `RESEARCH_RUNTIME_LIVE_BLOCKED`
and `PRODUCTION_BLOCKED` on the orchestrated path.

For historical implementation details, see
[P4.8 release audit and remaining gates](P4_8_RELEASE_AUDIT_AND_REMAINING_GATES.md).
