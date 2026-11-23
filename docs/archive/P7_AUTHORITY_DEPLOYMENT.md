> **历史档案**：此处保留迭代期的记录，不是当前设计或生产授权依据。请优先阅读 [整体设计](../../design/CURRENT_DESIGN.md) 和 [统一验收](../RELEASE_ACCEPTANCE.md)。

# P7.1 isolated authority: operator deployment contract

**State: deployable source and offline verification, NOT installed production
infrastructure.** This is a local, independent, audit/recovery authority
service. No API can dispatch a paid LLM request, start a research BRAIN
session, simulate, mutate an Alpha, or submit an Alpha. The Global Stop stays
irreversible. Never equate this local service's readiness with production
admission.

## What is delivered

- `deploy/p7/Dockerfile`: pinned Python runtime, non-root UID 65532,
  mandatory mTLS and separately protected API/admin Bearer secret files,
  built-in **read-only** SQLite health check.
- `deploy/p7/compose.yaml`: **Linux-only host networking**, binding the
  actual Python server to `127.0.0.1:8765` inside the **host network**.
  No ports are published, and there is no public-listening socket.
  Root filesystem read-only; caps dropped; no-new-privileges; private
  config mounted read-only; durable SQLite state mounted separately.
- `deploy/p7/config.template.json`: non-secret field/path reference, not a
  valid configured deployment. No secret key/certificate/token is committed.
- `research_authority_deployment.py`: strict owner-only file secret loading
  and bounded protected TLS key check. Rejects ambiguous file+environment
  tokens, same API/admin token, missing TLS/PKI, symbolic-link token files,
  malformed tokens and invalid permissions.
- `research_authority_server.py --check-ready`: opens its recovery SQLite
  database **read-only**, checks integrity, schema and persistent global Stop,
  and checks current secret/PKI material. Does not initialize a database,
  read any BRAIN/model secret or contact any network resource.
- `tests/test_p7_authority_deployment.py`: no real Provider/BRAIN/Docker,
  covers missing, invalid and owner-unprotected credentials, absence of
  startup side effects, persistent Stop and no-positive-permission output.

This is **not** an implementation of an independently deployed remote
production mTLS service. An operator still owns the host, private keys,
certificate trust roots, rotation, backup, restore, audit and ACL policy.

## Security boundaries

The server executable, container and client all keep their local-loopback
restriction. With Docker's Linux `network_mode: host`, the server's
`127.0.0.1` bind is the Linux **host loopback** (other hosts cannot connect
through a regular TCP interface). Do **not** change the bind to
`0.0.0.0`, publish container ports or use a public reverse proxy as a
workaround: those changes are outside the reviewed trust boundary.

The authority's two transport tokens have distinct roles:

- `api-token`: local read/preflight/one-time consumption/spend reserve,
  never remote provider execution.
- `admin-token`: irreversible Stop, revocation and remote-observation
  reconciliation; keep separate operator custody.

Transport tokens are *not* the Ed25519 Grant signers, Ledger signer, client
certificate identity, Provider API key or BRAIN cookie. The private
Consumer/Ledger signing keys are distinct and cannot be the operator/recovery
keys. TLS server identity and client CA must be independently provisioned.
No real model/BRAIN credentials belong in this service's filesystem or env.

## Operator provisioning (Linux only)

From the repository root, prepare a dedicated host/VM with a restricted
operator and a separate 65532:65532 container runtime identity.

1. Provision the operator Grant trust JSON, distinct Consumer/Ledger
   Ed25519 private keys, server certificate + matching private key, and
   the **client** trust CA. Use the existing signed Grant trust contracts in
   `docs/P5_RESEARCH_AUTHORITY_LOCAL_PILOT.md` and
   `docs/P6_INTEGRATION_RUNBOOK.md`; no fixture signing keys are
   accepted as production credentials.
2. Create private and state directories **outside a shared home folder**
   or under the ignored `deploy/p7/private` and `deploy/p7/state` paths.
   Both bind paths must exist before running Compose. The directory modes
   must permit the non-root container process to traverse/open them.
3. Place `config.json` in `deploy/p7/private`, copying only the **field
   structure** from `deploy/p7/config.template.json` and using the actual
   operator-provisioned identities and signed spending cap. It must have
   all three TLS fields. Container-internal paths are already defined in
   the template.
4. Supply `api-token` and `admin-token` as **different cryptographically
   random URL-safe strings**. Example for generating the values locally
   (run twice, direct output into the appropriate protected file):

   ```bash
   python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
   ```

   Use an approved secret manager to protect the generated tokens; never put
   them in shell history, GitHub Actions secrets, issue attachments, or logs.
   **All private tokens, key files and database contents must be readable
   or writable only by the intended UID**. On POSIX the source secret/key
   file must have mode `0600` and be owned by UID 65532. The directories
   should be mode `0700` owned by UID 65532, with the database volume
   writable by that UID. Verify mapping in rootless Docker environments.
5. Build/run only on a Linux host where Docker host networking has the
   expected loopback semantics:

   ```bash
   docker compose -f deploy/p7/compose.yaml config
   docker compose -f deploy/p7/compose.yaml build
   docker compose -f deploy/p7/compose.yaml up -d
   docker compose -f deploy/p7/compose.yaml ps
   ```

   These commands are **operator instructions only**; no service has been
   deployed by an assistant. Do not mount host Docker sockets or attach
   writable source code. Harden the host and ensure unprivileged UID mapping.

The Docker image intentionally contains **no** PKI, tokens, BRAIN credentials
or Provider billing credentials. `.gitignore` and `.dockerignore` exclude
private mounted paths, key files, local research data and SQLite sidecars.
The Compose service does not contain any placeholder token value to
accidentally accept.

## Availability and audit-only recovery

The container health probe uses the same checked deployment settings:

```bash
python3 -m alpha_research.orchestration.research_authority_server \
  --config /run/alpha-research/config.json --port 8765 \
  --require-mtls --require-file-secrets --check-ready
```

This probe returns only:

- `ready=true, stop_state=ARMED, status=LOCAL_AUDIT_ONLY`: the local
  database and security material can be opened; **no dispatch permission**.
- `ready=true, stop_state=STOPPED,
  status=LOCAL_RECOVERY_ONLY_STOP_LATCHED`: operator recovery remains
  available; **stop does not reset after a restart**.
- `ready=false`: storage missing, malformed schema, failed integrity
  probe or invalid Stop record. The probe must not create/reinitialize the
  state database; the container is unhealthy.
- Invalid/missing secret files or TLS materials fail closed with a sanitized
  error and nonzero exit; no fallback to plaintext TLS or inline bearer
  credentials when the strict flags are set.

The `global_budget_policy_initialized` field is intentionally separate from
`ready`. Before any signed Grant is reserved, the immutable spending cap
may not yet be recorded in the spend ledger. This **does not** mean a real
model or Simulation can run. Unknown model attempts stay quarantined and
their reserved budget is never automatically refunded.

Take encrypted, access-controlled backups of SQLite **including WAL data**
using a proper consistent SQLite backup procedure. Never restore a database
by replacing files while the service is running; never treat a restored
`STOPPED` ledger as `ARMED`. Do not automatically retry unconfirmed
Provider requests after a restart or a lost ACK.

## Disposable Docker execution rehearsal in CI

The Ubuntu P7 CI job first validates Compose and builds the real P7
image, then runs `python deploy/p7/container_smoke.py`.

This **does start a local disposable container on the GitHub runner**, but
never deploys to a user/server and never uses real credentials. It generates
throwaway Ed25519 signers, a short-lived CA, separate server/client TLS
identities and distinct API/admin tokens. It mounts those files into a
non-root, host-loopback-only container with a persistent test SQLite volume,
and checks:

1. TLS-authenticated read-only monitor returns `permission_to_dispatch=false`.
2. The container's internal read-only readiness probe sees `ARMED`.
3. Admin Stop latches `STOPPED` exactly once.
4. The probe still reports recovery availability after Stop.
5. Killing/restarting the container against the same test volume retains
   `STOPPED` and does not restore live authority.

Disposable secrets are never printed or uploaded; the fixture directory is
cleaned up after validation. This establishes a **sandbox deployment
rehearsal**, not independent production PKI, operations or billing acceptance.

## Acceptance still required outside the repository

- Docker build/boot in the independently managed deployment host;
  host loopback connection and OS secret ownership validation;
  certificate rotation and client-auth tests.
- Operator confirmation of CA custody, immutable host storage, database
  backups, on-call recovery and loss-of-response procedures.
- Independent **real Provider invoice/token readback** and hard credit cap;
  **real BRAIN read-only session admission**; supervised Canary.
- Explicit, separately scoped authorization decisions for any BRAIN
  Simulation POST. Automatic Alpha submission is prohibited by project policy.

Until these are complete: `PRODUCTION_BLOCKED`. The code in this batch
does not supply a config option that turns this status into authorization.
