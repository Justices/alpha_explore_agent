# P4.8 — Offline release audit and remaining external production gates

**Current code state:** implemented in the feature branch and checked by
cross-platform offline regression. **Production release remains BLOCKED.**

This stage brings together the existing Task/Proposal/Model identity,
Simulation scope staging, externally signed **consent audit**, non-executable
Escrow, ModelCallLedger and Simulation Intent. It does not install a real
production authorization issuer or allow Gateway writes.

## Commands (all read-only)

```sh
python3 -m alpha_research --lead deepseek --executor deepseek \\
  orchestration-release-gate <TASK_ID>

python3 -m alpha_research --lead deepseek --executor deepseek \\
  orchestration-simulation-recovery-audit <TASK_ID>

python3 -m alpha_research --lead deepseek --executor deepseek \\
  orchestration-simulation-dispatch-audit <TASK_ID>

python3 -m alpha_research --lead deepseek --executor deepseek \\
  orchestration-predispatch-escrow-status <TASK_ID>
```

`orchestration-release-gate` calls the existing read-only admission
inspectors and returns the full set of explicit blockers. It never
issues a consent receipt, creates an execution token, starts a Gateway,
creates a platform session, or calls a model. Output remains
`PRODUCTION_BLOCKED` or `RECONCILIATION_BLOCKED` and contains
`production_authorized=false`, `automatic_retry_allowed=false`.

## Recovery semantics

`simulation_recovery_audit.py` reports the saved Intent status without
transitioning it or calling BRAIN:

| Saved Intent | Read-only interpretation |
| --- | --- |
| PREPARED, complete offline escrow | Offline evidence only; no POST permission |
| POSTING | Remote outcome unknown, independently authorized readback required |
| OUTCOME_UNKNOWN | Remote outcome unknown, independently authorized readback required |
| RESPONSE_RECEIVED | Verify source/provenance and local response |
| READ_BACK | Review independently authenticated receipt and provenance |
| Missing or changed evidence | RECONCILIATION_REQUIRED |

A timeout, process crash, partial file write or inability to read a
platform response **must not** trigger a second POST. A saved response
digest is not independent proof of what the remote platform executed.

This read-only check is distinct from legacy workflow recovery, which
may use existing Gateway operations outside the new guarded orchestration
boundary. That legacy path must not be described as admitted P4.8
production execution.

## Gateway / Driver contract

The guarded positive read allowlist and forbidden execution claim keys
now live in
`alpha_research/integrations/brain/guarded_protocol.py`.
Both the Gateway and subprocess driver independently enforce these
identical constants. A read request containing a forged `grant_id`,
`attempt_id`, `authorization_id`, `allow` or
`permission_to_dispatch` is blocked, as are all guarded write commands.

The older non-guarded Gateway entrypoints remain separate and are not
covered by this new contract. Deployments must explicitly scope which
entrypoint is allowed rather than assuming the guard applies to legacy
callers.

## Model budget evidence and stop safety

The model budget ledger rejects both file and directory symlinks,
including dangling symlinks whose `exists()` value is false.
A missing ledger is an empty *offline* baseline only: the release gate
does not claim historical spend is proven. Global/launch stop signals
count dangling symlinks as active and refuse following symlinks when
writing an active stop flag.

## Available internal deliverables

- Scoped, immutable Simulation staging and global anti-replay index
- Independent Ed25519 verification of externally supplied receipt
  against caller-supplied public trust policy (**not** an attested issuer)
- One-use consent audit reservation and operator approval ID index
- One-use pre-dispatch Escrow with exact evidence fingerprints
- Negative-only Gateway and driver admission and forged-claim tests
- Read-only UNKNOWN/POSTING recovery classifier and release checklist
- Cross-platform offline tests for crash windows, concurrent writers,
  symlink attacks, double reservation, budget drift and stopped runs

## What cannot be marked done by code changes alone

1. **Authenticated operator-consent issuer**: a real external service,
   strong identity, secure private-key custody, review/audit trail,
   revocation and independently provisioned trust roots
2. **Financial controls**: account-level cost/currency ceilings,
   spend enforcement and verifiable model/platform billing history
3. **Live single-use grant consumption**: a separately reviewed
   trusted authorization service, independent driver-side enforcement
   and atomic durable POST claim with no bypass via raw stdin
4. **Remote operation verification**: authorized BRAIN account/session
   access, stable operation identifiers, idempotency/readback semantics,
   timeout/crash drill and remote reconciliation evidence
5. **Production canary**: explicit operator permission for one bounded
   real Simulation after the above controls are verified
6. **Alpha submission**: a distinct authorization and policy gate;
   Simulation approval cannot authorize an Alpha submission
7. **Release/merge**: code review and branch merge; no automatic
   modification of `main` is performed by this stage

The project intentionally **does not** expose a `--live` boolean or
promote an offline Escrow to a live grant. No model key, BRAIN credential
or private signing key should ever be committed to the repository.

## Validation

The CI pipeline runs Python 3.11 on Ubuntu and Windows, Python 3.11 and
3.13 on macOS, plus the AGY SDK contract. The core command is equivalent
to `python -m unittest discover -s tests` with the repository's offline
configuration. CI passing demonstrates that the tested local contract
holds; it does not prove real platform authorization or remote effects.

**Verdict:** Internal offline safety architecture can be completed and
regression tested. Production availability depends on a separately
deployed trusted issuer/consumer, authenticated platform access and
explicit canary authorization.
