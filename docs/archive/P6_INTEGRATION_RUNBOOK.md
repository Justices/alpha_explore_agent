> **历史档案**：此处保留迭代期的记录，不是当前设计或生产授权依据。请优先阅读 [整体设计](../../design/CURRENT_DESIGN.md) 和 [统一验收](../RELEASE_ACCEPTANCE.md)。

# P6.1–P6.6 integration runbook (offline evidence, production blocked)

This phase implements and tests the *local/offline contract* for the proposed
real-run integration. **None of the steps here provisions a production
identity, performs paid LLM calls, performs a live WorldQuant BRAIN request,
authorizes Simulation, or submits Alphas.** Do not equate local success with
approval to operate a real account.

## P6 implementation matrix

| Phase | Delivered in repository | Production blocker |
|---|---|---|
| P6.1 Identity and deploy | Non-root pilot Dockerfile; optional TLS 1.2+ client-certificate-authenticated loopback authority, separate Bearer API/admin roles, strict bounded PEM paths, and real ephemeral-PKI handshake tests | Externally managed CA/client identity, server provisioning, mTLS supervision, trusted secret custody, deployment and audits |
| P6.2 Real SDK adaptation | Task/role-bound registered OpenAI-compatible provider contract; pin model, HTTPS origin, exact options, output tokens, no retries; reuse real installed OpenAI SDK with in-process MockTransport | Real paid SDK dispatch intentionally disabled; cannot verify provider hard output caps or tokenizer |
| P6.3 Token/billing | Exact integer micro-USD computation using signed rates, token ceilings, discrepancy detection, signed provider readback and local operation ID correlation | Independently authenticated provider invoice API and real billing reconciliation not implemented; no capacity refunded |
| P6.4 BRAIN Read Only | Exact request allowlist and Task scope checking; mutation and cross-scope frame rejection | Real authenticated BRAIN research driver stays stopped before credentials; only pre-existing explicit non-P6 driver modes operate under their contracts |
| P6.5 Canary | Independent latched stop; monitor and SDK stop check; offline Canary audit and fault injection | Requires separate authorized supervised live read-only Canary, watchdog and operational rollback |
| P6.6 Release | Integrated negative-only P6 matrix inside top-level production release gate | Always PRODUCTION_BLOCKED until evidence/controls exist; no bypass switch |

The P6 inspection module is
[`alpha_research/orchestration/research_p6_readiness.py`](../../alpha_research/orchestration/research_p6_readiness.py).
All returns explicitly include `permission_to_dispatch=false`.
The implementation uses the already reviewed P4/P5 signed operator Grant,
single-use ACK, expense reservations, remote recovery observations and stop.

## Isolated local authority with mTLS

The pre-existing local pilot may still run as before using loopback HTTP with
distinct per-role Bearer tokens for local unit tests. To additionally require
mutual TLS, supply all three certificate settings in the existing operator
configuration file:

```json
{
  "schema_version": 1,
  "database_file": "/var/lib/alpha-research/authority.sqlite3",
  "operator_trust_file": "/run/alpha-research/operator-trust.json",
  "consumer_private_key_file": "/run/alpha-research/consumer-key.pem",
  "ledger_private_key_file": "/run/alpha-research/ledger-key.pem",
  "consumer_id": "consumer",
  "consumer_key_id": "consumer-key",
  "ledger_id": "remote-ledger",
  "ledger_issuer_id": "separate-ledger",
  "ledger_key_id": "ledger-key",
  "global_max_usd_micros": 10000,
  "server_certificate_file": "/run/alpha-research/server.crt",
  "server_private_key_file": "/run/alpha-research/server-key.pem",
  "client_ca_file": "/run/alpha-research/client-ca.crt"
}
```

Optional `recovery_trust_file` may also be supplied. Every referenced file
must exist and have the absolute path expected inside the process/container.
The Ed25519 private keys need owner-only permissions on POSIX; certificate
private keys must be protected by a separately provisioned filesystem and
process identity. The program does **not** generate/accept production PKI
identities. Never commit real private keys, tokens or certificates.

The client needs a CA file verifying the authority server plus its own
certificate/private key. Construct and pass its context through
`make_authority_mtls_client_context`:

```python
from alpha_research.orchestration.research_p6_mtls import (
    make_authority_mtls_client_context,
)
from alpha_research.orchestration.research_authority_client import (
    LoopbackAuthorityClient,
)

tls = make_authority_mtls_client_context(
    certificate_file="/path/to/readonly-client.crt",
    private_key_file="/path/to/readonly-client-key.pem",
    server_ca_file="/path/to/server-ca.crt",
)
client = LoopbackAuthorityClient(
    "127.0.0.1", 8765,
    api_token="<TOKEN_FROM_SECURE_SECRET_MANAGER>",
    tls_context=tls,
)
# No provider or BRAIN network operation; returns audit-only counters.
print(client.canary_monitor())
```

The local server still binds only 127.0.0.1; TLS does not widen its address
scope. The unit test generates a temporary CA, server certificate (with the
127.0.0.1 IP SAN), and separate client certificate. It tests accepted
handshakes, missing client certificates, untrusted server CA and the
unchanged stop/negative permissions.

### Optional networkless container build

```bash
docker build -f deploy/p6/Dockerfile -t alpha-research-authority:p6-offline .
```

**No container is started or deployed by CI.** An operator who wants a
network-isolated local acceptance sandbox may run this image with
`--network none`, a read-only root filesystem, non-root UID 65532 and
explicitly mounted private config, writable state database and protected
keys. The image contains no sample credentials and no live BRAIN/LLM
dispatch. Do not publish container ports or attach unrestricted outbound
networks. Container-level database ownership and storage persistence are
the operator's responsibility.

## P6.2 provider boundary and P6.3 billing

`inspect_p6_provider_plan(...)` accepts only registered compatible backends
whose pinned HTTPS host matches the supported set:

- `api.deepseek.com` (DeepSeek);
- `open.bigmodel.cn` (GLM BigModel);
- `api.moonshot.cn` (Kimi).

The URL and model still come from the signed Task/role profile; callers
cannot freely change the host/model/endpoint. This function does **not**
open the HTTPS URL, read the API key or enable live model dispatch.

`inspect_p6_provider_metering(...)` recomputes a conservative micro-USD
estimate from provider-reported input/output tokens using signed per-million
rates. It rejects missing, inconsistent or over-cap token counts. If an
invoice amount is provided for **offline comparison**, it reports a
difference or budget violation; the amount is still **untrusted** and
does not become independently authenticated billing. No budget
reservation is refunded.

`inspect_p6_signed_provider_readback(...)` validates the externally signed
report against the specific Task, Grant, ACK, attempt ID and previously
persisted Provider Operation ID. Even a valid signature is **audit only**
until a real provider identity and invoice can be independently established.
A failed or unknown attempt is never automatically retried.

## P6.4 BRAIN read-only contract

The pure frame-inspection entrypoint
`inspect_p6_brain_catalog_frame(...)` accepts only declared read actions
(`get_datasets`, `get_datafields`, `get_operators`, etc.), enforcing exact
Region/Delay/Universe/Instrument and optional source Alpha scope. Unknown
commands, Simulation, Alpha mutation and user-provided execution claims
are refused. No BRAIN session is created. The existing
`driver.py --research-catalog` executable remains blocked before credential
read until independent trusted read-only admission can be implemented and
separately authorized.

## Integrated offline E2E rehearsal

`run_p6_offline_canary(sdk_inputs=..., attempt_id=..., brain_read_frame=...)`
composes the real installed OpenAI-compatible Python SDK *with its in-process
HTTP MockTransport*, the P5.1 independently operating authority's signed
Task/ACK reservation and stop preflight, the P6 provider contract,
integer token-based cost audit, BRAIN Catalog frame allowlist and the P6
negative release matrix. It never constructs a BRAIN session or routes HTTP
to a paid provider.

The P6 regression asserts **exactly one** synthetic ChatCompletion request,
zero automatic retries, persisted provider operation ID, retained USD
reservation after success, and an unconditional production-blocked outcome.
Mutating BRAIN frames fail *before* a budget reservation is requested.
Replaying the same model attempt ID fails closed.

## P6.5 dry-run Canary and P6.6 release

`inspect_p6_integrated_release` composes the P5 gate with individual
P6 provider, metering, signed remote readback and BRAIN frame inspections.
It returns a six-stage status map and explicit outstanding blockers.
It never turns locally supplied or test-generated evidence into production
rights. The top-level `inspect_production_release_gate` now includes its
negative report for Super/Optimization tasks.

Local emergency-stop `STOPPED` remains irreversible and prevents new
budget reservations. A successful TLS handshake or 'ARMED' state alone
is **not** an authorization to dispatch any live call.

## Verification

```bash
python3 -m pip install -e .
python3 -m compileall -q alpha_research tests
python3 -m unittest discover -s tests -p 'test_p4_research_final_release.py' -v
python3 -m unittest discover -s tests -v
```

The CI matrix covers Linux, Windows, macOS Python 3.11 and 3.13, including
real TLS handshake tests, synthetic signed price/operation reconciliation,
JWT-free strict per-role local auth, malformed input rejection, and a negative
top-level release gate.

### External sign-off required to leave PRODUCTION_BLOCKED

1. Independently provisioned and audited service identity/credential custody,
   trusted transport and isolation, stable deployment/monitoring.
2. Provider-specific legal pricing and account/invoice API integration,
   verified tokenization and enforceable spending controls.
3. Scoped BRAIN authentication/session with separate read-only approval.
4. A supervised real Canary with explicit operator approval, safe stop
   propagation and documented incident response.
5. Separate permission decisions for any real Simulation POST and Alpha
   submission. Existing architecture explicitly forbids automatic submission.

**P6 offline code and contract tests are implementable here; external
production deployment and real account acceptance cannot be claimed
from a GitHub branch or mocked fixtures.**
