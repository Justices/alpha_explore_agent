# Model runtime governance (P2)

This stage adds **local readiness checks**, **durable research-scoped model
call budgets**, and **manual-only model switching**. None of these tools
authorizes live BRAIN Simulation or Alpha submission.

## 1. Local doctor (no external requests)

```powershell
python -m alpha_research --lead codex --executor glm models doctor
python -m alpha_research --lead kimi --executor claude-code models doctor
```

Doctor only checks CLI executable discoverability, SDK package presence,
HTTPS URL shape and whether the required credential environment variable
has a value. It never starts a CLI/SDK process, calls a model, contacts BRAIN,
tests credential validity, or measures real provider availability.
`READY_LOCAL` means **local prerequisites**, not provider connectivity.
Missing prerequisites return `BLOCKED_LOCAL` and CLI exit code 2.
Actual credential values are not printed or persisted.

## 2. Configured call budget

Add this top-level object to your research configuration:

```json
{ "model_governance": {
    "max_calls": 80,
    "max_reported_tokens": 250000,
    "failover": "manual"
} }
```

The policy is explicitly included in config.example.json. It is enforced
for recorded Regular, Super and Optimization model calls. Older or custom
callers without the policy do not gain limits magically; integrate them
through DecisionRecorder(..., governance=policy).

Calls are reserved in an on-disk, per-research ledger **before** invoking
any CLI, SDK or API. Both successful and unsuccessful attempts consume one
hard call quota. Attempt records are locked, checksummed, and immutable
across policy changes within the same research ID.

Token metering uses **only the tokens the provider actually reports**.
`max_reported_tokens` prevents another request once known cumulative tokens
have reached the threshold. It cannot constrain the current request's output,
unknown usage, price, or eventual charges. No cost estimation is fabricated.

The model role, selected registered backend ID and effective backend config
fingerprint are bound in the ledger. Resuming a research ID after modifying
the selected model or runtime fails closed. Use a new research ID instead.

## 3. Inspect usage without provider calls

```powershell
python -m alpha_research --state-root D:\alpha-state `
  --config .\config.example.json models budget --research-id R1
```

The report displays total attempts, success/failure/unknown counts,
unresolved intents, reported tokens, unknown-usage attempts and blocked
status. All values come from local persisted audit state.

## 4. Unknown outcome and manual failover

A process crash after reserving a model call leaves an `IN_FLIGHT` intent.
It is **not** automatically retried or moved to another registered model.
Inspect the model audit and platform state manually first. If the original
response cannot be recovered, acknowledge the uncertainty explicitly:

```powershell
python -m alpha_research --state-root D:\alpha-state `
  --config .\config.example.json models acknowledge-unknown `
  --research-id R1 --call-id CALL_ID --reason "Response could not be verified"
```

This operation does **not** assert that the call failed. It changes the
local attempt to `UNKNOWN` and permanently blocks further model work under
that research ID. Any new attempt, including a different model, must start
a distinct research ID and follow existing experiment recovery rules.
No model call, BRAIN call or Alpha submission occurs in this operation.

Manual failover does not imply resubmitting potentially posted simulations.
The independent BRAIN Simulation Intent/Recovery gate must reconcile all
pending platform writes before a separate new study begins.

## Testing

tests/test_model_governance.py covers local doctor, backend presence,
pre-call reservations, hard attempt caps, reported-token thresholds,
model identity pinning, restart behavior, corrupted state, uncertain calls,
manual acknowledgement and CLI output. GitHub Actions runs this on Linux
and Windows. No live provider requests are part of this P2 verification.

## Known limits

- Local doctor does not prove remote service health or authentication.
- Dollar costs, currency and real price budgets are not yet available.
- A request can exceed the reported-token threshold before the next check.
- Model governance is not a replacement for BRAIN execution authorization.
- Real CLI/SDK integration and production failure modes still need separate
  authorization and end-to-end smoke tests.
