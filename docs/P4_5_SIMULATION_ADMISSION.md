# P4.5.1 / P4.5.2 — Simulation admission scope and safe pre-POST lifecycle

This stage establishes a strict simulation-specific verification contract
and serializes durable Simulation Intent transitions. It is **offline-only
integration work**: no real model, BRAIN Simulation or Alpha submission is
authorized by these components.

## 1. Scope-bound receipt verification

`simulation_scope.inspect_simulation_scope` and the read-only
`orchestration-simulation-scope-check` command verify an externally signed
v2 receipt, distinct from the earlier v1 generic receipt.

A receipt binds:

- a fresh, one-attempt `CREATED` orchestration task and immutable
  request/proposal/profile/governance hashes
- the exact workflow, domain `research_id`, `run_id` and `batch_id`
- the complete simulation `request + settings` fingerprint
- exact Region / Delay / Universe / EQUITY instrument scope already pinned
  in the prepared task, without broadening missing scope
- UTC issuance and expiry with a maximum 15-minute lifetime, one attempt,
  and `brain_simulation` as the only recognized operation

The receipt uses a separate HMAC context and the environment variable
`ALPHA_RESEARCH_SIMULATION_SCOPE_VERIFY_KEY`. This repository **does not
issue these receipts or establish user consent**; the local verifier secret
alone cannot create an execution permission.

A successful check returns
`SCOPE_VERIFIED_NOT_AUTHORIZED`, with
`permission_to_dispatch=false`. It does **not** create a Simulation
Intent, reserve a live attempt, connect to BRAIN or authorize a POST.

### CLI

```sh
python3 -m alpha_research --lead deepseek --executor deepseek \
  orchestration-simulation-scope-check <TASK_ID> \
  --receipt /secure/path/simulation-receipt-v2.json \
  --manifest /secure/path/simulation-manifest.json
```

Manifest shape (illustrative synthetic values; not a real BRAIN request):

```json
{
  "research_id": "super:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "run_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "batch_id": "batch_one",
  "request": {"selection": "rank(x)", "combo": "rank(y)"},
  "settings": {
    "instrument_type": "EQUITY",
    "region": "USA", "delay": 1, "universe": "TOP3000"
  }
}
```

The prepared task must already pin USA / D1 / TOP3000 and match every
claim. The complete production simulation settings schema may contain many
additional fields; the manifest is an identity preflight, **not** a valid
platform request on its own.

## 2. Durable Intent lifecycle: single writer and no silent replay

`simulation.intent.create_simulation_intent` and
`transition_simulation_intent` now take a path-specific cross-process
writer lock, with duplicate detection and state recheck under the lock.
A concurrent creator or POST reservation is refused rather than gaining
a second attempt.

Allowed state progression remains:

```
PREPARED -> POSTING -> RESPONSE_RECEIVED -> READ_BACK
                \-> OUTCOME_UNKNOWN
OUTCOME_UNKNOWN -> RESPONSE_RECEIVED  (verified readback only)
```

The local state machine never automatically transitions from unknown
outcome to POSTING or resubmits a simulation. An
`OUTCOME_UNKNOWN -> RESPONSE_RECEIVED` transition now requires an existing,
non-symlinked response JSON file under the Intent directory, plus a matching
SHA-256 fingerprint. This is a **local evidence-integrity check**, not proof
that the remote platform really performed the operation; separately
authenticated readback is still mandatory. The lock serializes local writers;
it does not by itself provide remote idempotency.

## 3. Atomic one-shot pre-dispatch staging (new)

`simulation_attempt_admission.py` persists a canonical task/authorization
staging journal **before** creating its associated Simulation Intent. The
staging file is durable and non-executable: its state is always
`STAGED_NO_DISPATCH` with `permission_to_dispatch=false`. The
cross-process reservation lock serializes all tasks in the state root;
duplicate authorization IDs (even on other tasks) are rejected.

If a crash leaves a journal without a complete, matching PREPARED Intent,
`orchestration-simulation-stage-status` returns
`RECONCILIATION_REQUIRED`. An automatic second staging or POST is forbidden.
The global stop flag and the mock-workspace marker also block staging.

```sh
# Only after obtaining a separate externally issued v2 scoped receipt:
python3 -m alpha_research --lead deepseek --executor deepseek \
  orchestration-simulation-stage <TASK_ID> \
  --receipt /secure/path/simulation-receipt-v2.json \
  --manifest /secure/path/simulation-manifest.json

# Status inspection does not consume additional rights:
python3 -m alpha_research --lead deepseek --executor deepseek \
  orchestration-simulation-stage-status <TASK_ID>
```

This is a **non-executable audit reservation**, not trusted approval
consumption. Even a successful stage does not authorize model, BRAIN POST,
readback, or Alpha submission. Its evidence is surfaced by the ordinary
`orchestration-admission-audit` as an explicit blocker.

## 4. Gateway and driver guarded admission boundary

The new orchestration-specific Gateway must be constructed with
`Gateway(orchestration_guarded=True)`. It passes
`--orchestration-guarded` to the driver. **Both layers independently reject**
`simulate_batch`, `simulate_super`, `tag_alpha`,
`sync_backtest_cache` and `resume_simulation_batch`, including requests
that contain untrusted `allow` or `authorization_id` fields.

The guarded mode is a compatibility-safe separate entrypoint for
orchestration development. Existing legacy non-guarded Gateway clients
are not magically controlled by it; converting those pathways safely
requires an independent, reviewed migration. No trusted issuer or
server-side live grant consumer has been installed, so a guarded Gateway
is deliberately **incapable of posting a Simulation**.

The Gateway and subprocess now both implement an **explicit read-only
allowlist** rather than a write-command blacklist. A future unknown
driver command is denied by default. Guarded driver error responses
redact remote exception payloads (credentials, account identifiers
and provider diagnostics) instead of echoing them.

`tests/test_p4_guarded_dispatch.py` verifies that forged `allow=true`
and `authorization_id` fields, direct stdin injection, unknown future
commands and provider exceptions cannot bypass this deny-by-default
boundary.

The read-only CLI
`orchestration-simulation-dispatch-audit <TASK_ID>` combines the task
budget, staged Intent integrity, global stop, mock workspace status and
model budget ledger with explicit missing-authorization blockers.
It never signs, consumes or executes an operation.

## 5. Outstanding production gates

P4.5.1 is **not production-enabled**. The following must exist and be
independently audited before a live Simulation:

1. A trusted approval issuer that verifies explicit user consent for one
   charged experiment and controls signing-key custody.
2. An atomic **live** attempt journal and trusted server-side consent
   consumption; the P3 receipt reservation is audit-only and cannot be
   promoted to a dispatch capability.
3. A Gateway/driver-side operation-specific authorization check tied to
   the exact Simulation Intent, Task ID and unique attempt ID.
4. A verified mapping to ModelCallLedger and persistent Research ID,
   with no ambiguous budget refunds.
5. An authorized BRAIN readback-only reconciliation path for lost POST
   responses and a concrete rollback/stop protocol.
6. Approved one-experiment canary criteria. No automatic submission.

No global `--live` shortcut or environment boolean may bypass these gates.
P4.4 read-only connectivity to USA / D1 / TOP3000 does not imply
permission for even one Simulation.

## 6. Offline acceptance

`tests/test_p4_simulation_staging.py` covers the canonical journal,
cross-task authorization-ID reuse, concurrent reservations, and interrupted
journal/Intent writes. `tests/test_p4_simulation_scope.py` covers signature invalidity, scope and
request drift, attempt budgets, expiry, and concurrent Intent creation /
PREPARED-to-POSTING transitions. All existing workflow and Intent recovery
tests must continue passing on the CI matrix.

`tests/test_p4_guarded_dispatch.py` verifies Gateway-side rejection,
direct driver stdin bypass rejection, propagation of the guarded-driver
flag and negative pre-dispatch audit with stop signal.

Actual live operations remain out of scope for this stage.
