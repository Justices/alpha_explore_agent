> **历史档案**：此处保留迭代期的记录，不是当前设计或生产授权依据。请优先阅读 [整体设计](../../design/CURRENT_DESIGN.md) 和 [统一验收](../RELEASE_ACCEPTANCE.md)。

# Research Runtime — Unified Operator Guide (P4.19–P4.29)

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

## P4.25–P4.29 — External control-plane modules and Canary gate

P4 now has **five read-only/audit integration contracts**. All five reject
production dispatch by design. The existing
**orchestration-release-gate TASK_ID** command includes a
**research_p4_canary** component, listing the missing dependencies and the
reason real execution stays blocked.

| Module | New code | Implemented and verifiable offline | Still requires deployment outside this application |
| --- | --- | --- | --- |
| P4.25 Grant consumer | research_grant_ledger.py, research_grant_service_reference.py | Ed25519 ACK/ledger-readback binding; transactional SQLite reference consumer, unique grant/approval consumption, durable ACK, revocation and concurrent-writer tests | Independently hosted authenticated remote consumer, protected signing keys, distributed revocation and operational guarantees |
| P4.26 BRAIN Driver | research_driver_session.py | Signed session/scope/Grant ACK binding and exact driver-frame read-only allowlist including Optimization source Alpha | Actual independently authenticated driver session; the live research driver still refuses startup before credentials |
| P4.27 Cost and metering | research_price_provenance.py, research_spend_service_reference.py | Signed provider/model tariff matching the grant, bounded SDK plan; atomic per-Grant and cross-Grant USD reservation, immutable persisted budget policy, call cap, unknown-outcome quarantine | Independently verified current provider rates and tokenizer, API-enforced output caps, trusted global budget service and provider invoices |
| P4.28 Recovery | research_remote_reconciliation.py | Signed attempt-specific remote outcome validation; missing/unknown/in-flight/failure/cost-or-token excess never auto-retried | Real provider operation-ID readback, externally trusted billing settlement and durable incident/compensation operations |
| P4.29 Canary | research_p4_release.py, release_gate.py | One negative-only matrix for four evidence chains and signed Canary *review*, typed Task binding and stop checks | Separately approved supervised production canary, operator abort/monitoring and a genuinely enabled independently controlled execution plane |

**Important distinction:** reference SQLite ledgers model actual durable,
concurrent single-consumption and budget reservation logic, but running either
inside the Agent process does **not** make it an independent security service.
The production adapter, real SDK dispatcher, Catalog driver startup and
BRAIN write operations remain disabled. A user-supplied public key, signed
report, local SQLite record or offline test is not external authority.
A Canary review is explicitly **REVIEWED_NOT_ENABLED**, not a dispatch token.

All external evidence validators use independent Ed25519 signature domains,
exact claim fields, Task/receipt binding, strict JSON value types, short
expirations and revocation checks. The driver and price reports are still
**AUDIT_ONLY**; uncertain remote model outcomes lock out automatic retry. The
P4 Canary report always returns **RESEARCH_P4_CANARY_PRODUCTION_BLOCKED**,
even when all four signed audit reports and a Canary review are supplied.

To run the isolated verification tests (no API keys, remote services or real
BRAIN requests):

    python3 -m unittest discover -s tests -p 'test_p4_research_final_release.py' -v

Before any controlled deployment, implement a separately authenticated
service transport, custody and rotate independent keys, ensure trusted remote
ledger and cost service are actually used by the driver/SDK (not merely
reported by the client), enforce model-specific billable token semantics,
perform recovery readback and independently authorize a supervised read-only
canary. Simulation POST and Alpha submission remain distinct approvals.

## P5.3 — Offline SDK preflight, budget reservation and quarantine

The real OpenAI Python SDK is now used in an isolated end-to-end pilot
against an **in-process MockTransport**, using either the native HTTPX
(SDK 2.x) or HTTPX2 (SDK 3.x) transport family. A verified operator receipt,
global consumption ACK, separate ledger readback, and signed model tariff
are checked before the local authority reserves a bounded worst-case USD
amount. The SDK is fixed at `max_retries=0` and at most one ChatCompletion.

Malformed or truncated responses, exceeded token counts and synthetic
transport timeouts mark that reserved attempt `UNKNOWN` remotely and keep
the money reserved. Successful mock replies remain `RESERVED` until a
separately verified provider settlement mechanism exists. The pilot cannot
read API keys, connect to the network or authorize real SDK dispatch.
P5.3 does not modify the production release gate.

See [P5.3 SDK pilot and metering](P5_3_SDK_PILOT_AND_METERING.md).

## P5.4 — Crash-safe provider operation and recovery audit

The isolated authority now persists one unique SDK-returned provider
operation ID per Task/attempt and supports Task/ACK-bound read-only
attempt lookup after process restart, including following Grant revocation.
An optional **server-side** independent recovery signer can provide
Ed25519 remote-outcome observations, ingested only using the admin
credential into an append-only audit table. Duplicate evidence, conflicting
provider IDs, stale signatures and identity mismatches fail closed.

A signed remote report, even one labelled SETTLED, is **not** verified
billing or independent access to the actual provider. It never changes a
RESERVED/UNKNOWN attempt's spend state, releases the previously committed
budget, or permits a retry. Real provider identity/readback and invoice
settlement remain independent outstanding requirements. The production
release gate still returns PRODUCTION_BLOCKED.

See [P5.4 remote recovery and audit](P5_4_REMOTE_RECOVERY_AND_AUDIT.md).

## P5.5 — Local Canary controls and production barriers

The independent loopback authority has an irreversible persisted GLOBAL
stop and aggregate read-only telemetry. Budget reservations check the
stop under a single immediate SQLite transaction, and the offline SDK
MockTransport checks again after reservation. A stopped Grant can still
be audited/quarantined, but no new budget is committed. These controls
do not enable a real BRAIN/LLM consumer and are **not** a substitute
for live independent enforcement or an operator-approved Canary.

The negative-only P5 release gate reports outstanding service identity
and mTLS, signed live approval, authenticated provider readback/billing,
hard provider budget, incident supervision and separate BRAIN Simulation/
Alpha submission approval. See [P5.5 stop/release runbook](P5_5_CANARY_STOP_RELEASE.md).

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
