# P4.3 Production admission review — static, read-only

**Scope:** current `refactor/configurable-codex-agy-runtime` branch.
**Method:** inspect source contracts for model routing/credentials, operation
authorization, BRAIN Gateway, Simulation Intents and recovery. No paid model
calls, BRAIN session, live Simulation or Alpha submission performed.
This report is a **source audit**, not proof of the operator's local secret
configuration, remote provider access or production readiness.

## Decision: NOT AUTHORIZED for unified production execution

| Gate | Verified source behavior | Admission result |
| --- | --- | --- |
| Model route and local SDK/CLI readiness | `registry.py`, `backend_validation.py`, `doctor.py` select registered models and check local binaries/SDKs and credential environment presence. `runtime_config.py` validates the governance policy; built-in defaults are offline-only. | **Local readiness remains environment-specific**; remote provider permissions unverified |
| Model attempt budget | `ModelCallLedger.reserve` writes attempt evidence before provider calls; unresolved/unknown attempts block automatic retry or failover. Token limit is based on **reported** tokens, not a hard monetary ceiling. | **Offline contract present**; real attempts not exercised in this audit |
| Unified execution authorization | Proposal/identity and signed review receipt are non-authorizing. Signed execution receipt verification and `RESERVED_INSPECTION_ONLY` prevent duplicate audit reservation, but no trusted issuer/user-consent consumer or **live** atomic admission ledger exists. | **BLOCKED: trusted authorization and production dispatch not implemented** |
| BRAIN credential/session | `Gateway.start` owns the driver process/session and credentials; `Gateway.call` uses an explicit command whitelist. Authentication responses are now redacted from exposed errors. | **BLOCKED: session/auth and scoped live operation permissions unverified** |
| Simulation intent / uncertainty | `simulation/intent.py` persists request identity and state transitions PREPARED → POSTING → RESPONSE_RECEIVED → READ_BACK, and treats OUTCOME_UNKNOWN as non-retryable; recovery polls previously recorded operations. | **Offline contract present**; no live ambiguity drill |
| Orchestration recovery | `recovery_preflight.py` is read-only; recovery needs a valid durable intent, matching workflow, bound domain Research ID, saved ledger and independent reconciliation. | **BLOCKED pending live recovery handoff verification** |
| Candidate/Preferred/Submission | Offline provenance is excluded from candidate admission. Qualification and submission must be independently approved, not inferred from the mock success state. | **BLOCKED: no explicit submission authorization** |

## Source-level P0/P1 findings and changes

**P0 — Gateway error data exposure (remediated):** The Gateway previously
included raw driver authentication and provider error bodies in exceptions.
Driver results can include sensitive request/session details; error content
must not be reflected into local logs. Gateway startup and command failures
now use generic, non-secret-bearing messages; regression cases in
`tests/test_gateway.py` assert the raw sentinel is not exposed.

**P0 — Production admission still deliberately absent (release blocker):**
`ProductionWorkflowAdapter.run` refuses `validate`/execute without a
separate trusted execution grant; no change in this review enables it.
An inspected or reserved HMAC receipt is never a dispatch capability.

**P1 — Admission inspection lacked a local readiness view (remediated):**
`orchestration-admission-audit` now calls the existing credential-safe local
role readiness check and adds explicit `MODEL_ROLES_NOT_READY_LOCAL` (when
applicable), `BRAIN_AUTHENTICATION_NOT_VERIFIED` and
`LIVE_GATEWAY_OPERATION_GRANTS_NOT_BOUND`. No secret value is displayed;
`remote_health_verified` remains false.

## What this audit cannot prove

- Whether local DeepSeek/GLM/Kimi/Codex credentials are present on the user's
  Mac; GitHub-hosted code inspection does not read their local secrets.
- Whether the configured BRAIN account is authenticated, permitted to access
  each Region or eligible to submit a particular Alpha.
- Whether the remote endpoint and actual Simulation recovery semantics match
  scripted offline mocks.
- Whether the operator approved a concrete paid model call, Simulation or
  Alpha submission. Those permissions must not be inferred.

## Operator commands that are safe and local

These commands do not create BRAIN sessions or invoke model providers:

```sh
python3 -m alpha_research --lead deepseek --executor deepseek \
  preflight --region CHN

python3 -m alpha_research --lead deepseek --executor deepseek \
  orchestration-admission-audit <FRESH_PREPARED_TASK_ID>

python3 -m alpha_research --lead deepseek --executor deepseek \
  orchestration-recovery-check <BLOCKED_TASK_ID>
```

`preflight` can report `READY_LOCAL` when basic local prerequisites exist.
**That status does not grant simulation or any production execution.**

## Required next phase: P4.4 and beyond

1. Authorize a narrowly scoped, read-only BRAIN connection check after
   verifying the exact account/region and session-handling policy.
2. Build and separately review a trusted issuance and consumption mechanism
   for **specific** live operations, with explicit consent, TTL, attempt count,
   immutable Task/Profile/Policy identity and atomic write-ahead admission.
3. Enforce grants in Gateway and model-provider dispatch, not only in CLI;
   bind them to Research ID, ModelCallLedger and persistent Simulation Intent.
4. Reconcile unknown remote Simulation outcomes without POST replay; verify
   the operation with a bounded canary only after explicit permission.
5. Keep Alpha submission separately approved and RA/PC/SC and quota gated.

**Decision:** `NOT_AUTHORIZED` until blockers are resolved and acceptance
results are recorded against an exact Git commit. Passing offline tests is
necessary but not sufficient to authorize live execution.
