> **历史档案**：此处保留迭代期的记录，不是当前设计或生产授权依据。请优先阅读 [整体设计](../../design/CURRENT_DESIGN.md) 和 [统一验收](../RELEASE_ACCEPTANCE.md)。

# P5.4 — Durable model operation identifiers and signed recovery observations

**Status: offline reference integration only.** This phase improves post-crash
recovery and audit; it does **not** enable real provider calls, refund reserved
budget, or authorize BRAIN Simulation / Alpha submission.

## Main distinctions

- **Task attempt ID** (locally generated, immutable, 32 lowercase hex chars)
  uniquely identifies a requested model operation before SDK dispatch.
- **Provider operation ID** (returned by the SDK, or learned from a separately
  signed readback) is stored once in the service's durable SQLite journal.
  The plain SDK-provided ID is *unverified* until corroborated.
- **Signed remote outcome observation** is an external source assertion,
  cryptographically bound to the Task, Grant ACK, research receipt, attempt,
  and provider operation ID. The service verifies this against **its own**
  provisioned remote trust policy, never against a trust file sent by a
  client.
- **Provider invoice settlement** remains unimplemented: a signed observation
  is not an independently authenticated invoice, even if it says SETTLED.
  The existing reserved USD capacity is deliberately never refunded.

## Recovery process

1. Before a model request, atomically reserve a unique Task/attempt ID and
   upper-bound USD amount through P5.1's authenticated authority service.
2. In the P5.3 SDK MockTransport, read the synthetic ChatCompletion ID after
   the response, then persist its ID using **POST /v1/spend/operation**.
   A lost operation-binding response causes the client to quarantine the
   attempt; the SDK call is never repeated.
3. A transport failure, timeout, malformed response or uncertainty leads to
   **POST /v1/spend/quarantine**. The attempt remains UNKNOWN and its budget
   remains committed.
4. After process restart, call **POST /v1/spend/attempt** with the original
   Grant, Task, attempt and ACK digest. This is a strictly local, read-only
   ledger lookup and works after Grant revocation. A missing operation ID
   cannot be interpreted as proof that a remote request never started.
5. An independent remote readback signer can issue an Ed25519 observation
   using the existing `research-model-readback/v1` domain. A separately
   authenticated operator submits it through **POST /v1/spend/reconcile**
   using the **admin** credential. The service checks the signature,
   freshness, trusted issuer, Task/receipt/ACK/attempt bindings, token count,
   cost envelope and previously recorded provider operation ID. It stores
   the entire signed observation and verdict in an append-only journal.
6. A NOT_FOUND report must carry zero reported tokens and zero charge;
   contradictory evidence is rejected before journal ingestion.
   If the remote report says NOT_FOUND, IN_FLIGHT, UNKNOWN or FAILED,
   the result remains an audit/incident; a later signed SETTLED observation
   may be appended, but neither changes UNKNOWN to an executable state.
   If a claimed charge exceeds the original USD reservation, the audit
   records an over-reservation incident instead of marking settlement.
7. **No status in this pilot permits retry**; no replay of signed evidence
   IDs, provider operation IDs, or attempt IDs is allowed.

The remote observation is still **not proof of real provider access**.
In this pilot a test signer is intentionally used. A future live deployment
needs server-side operation-ID lookup against an authenticated provider,
independent invoice verification, service identity, operator-controlled
settlement policy and crash-safe error handling.

## Local authority API (loopback only)

| Endpoint | Required role | Result |
| --- | --- | --- |
| `POST /v1/spend/operation` | API | Bind one SDK-reported provider ID to one pre-reserved attempt |
| `POST /v1/spend/attempt` | API | Inspect committed attempt, operation ID, latest signed verdict, and retry prohibition |
| `POST /v1/spend/reconcile` | Admin | Verify and append externally signed remote readback; no settlement |
| `POST /v1/spend/quarantine` | API | Permanently quarantine a previously reserved uncertain attempt |

All paths return `permission_to_dispatch=false`. The authority does not
expose any API to settle, delete, or refund a committed spend reservation.

## Independent recovery trust configuration

The [P5 authority pilot config](P5_RESEARCH_AUTHORITY_LOCAL_PILOT.md) may
specify an *optional* `recovery_trust_file` containing an absolute path to
a separately provisioned JSON policy:

```json
{
  "schema_version": 1,
  "revoked_evidence": [],
  "issuers": {
    "remote-provider": {
      "key_id": "provider-readback-key",
      "public_key_b64": "BASE64_OF_32_BYTE_ED25519_PUBLIC_KEY",
      "enabled": true,
      "operations": ["observe_research_model_outcome"]
    }
  }
}
```

The signer public key must differ from the operator, Grant-consumer and ledger
readback keys. The service refuses reconciliation when the policy is absent,
malformed, revoked or does not validate the exact signed readback. This
optional trust policy never allows live provider dispatch; merely uploading
a signer key does not demonstrate independent provider custody.

Do not include private keys or API tokens in JSON. Never publish service
ports or expose the loopback HTTP server on the network. Use independently
managed service authentication and mTLS in any future production design.

## Offline regression

```bash
python3 -m pip install -e .
python3 -m unittest discover -s tests -p 'test_p4_research_final_release.py' -v
python3 -m unittest discover -s tests -v
```

The tests check persistent operation IDs, two independently signed readback
observations, duplicates and cross-Task forgery, admin credential isolation,
mismatched operation ID, restart persistence, revocation, UNKNOWN quarantine,
and invoice-over-reservation handling.

## Production release blockers

- Real provider operation discovery/readback and independently trusted
  invoice amount/price semantics have not been implemented.
- A provider-signed observation in this pilot is still **audit only**.
- The real SDK adapter, live driver and BRAIN write operations are still
  intentionally disabled.
- Per-provider tokenizer and output-cap enforcement, kill switch, supervised
  real read-only Canary, and separately authorized Simulation / submission
  remain prerequisite work.

**P5.4 adds auditability and fail-closed recovery, not live execution rights.**
