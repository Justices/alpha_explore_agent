> **历史档案**：此处保留迭代期的记录，不是当前设计或生产授权依据。请优先阅读 [整体设计](../../design/CURRENT_DESIGN.md) 和 [统一验收](../RELEASE_ACCEPTANCE.md)。

# P4.7.2 — External operator-consent signature and audit reservation

**Implementation state:** offline-only, production write dispatch **NOT AUTHORIZED**.
The existing P4.7.1 canonical Simulation staging index remains in place;
this stage introduces a distinct, independently verified audit record.

## Trust boundary

The repository does not sign grants, generate issuer private keys, serve an
approval UI, or certify that a human actually consented to a charge. The new
`operator_consent.py` module accepts externally signed Ed25519 receipts
with a separate, explicitly injected trusted-issuer public-key policy.
A signature proves possession of the corresponding issuer private key,
**not** operator identity, price approval or BRAIN execution permission.
A caller-supplied trust policy alone is not an operational trust root.

A real deployment must separately provision a trusted issuer, restrict its
private-key custody, establish authenticated human consent, enforce revocation,
audit the approval source and protect the trust policy against arbitrary
application writes. No such issuer, authenticated approval or production
provisioning exists in this implementation.

The key registry used in offline tests contains only synthetic generated
keys. Never commit real private keys or operator credentials.

## Scope of the signed receipt

The v1 external-consent receipt uses domain-separated canonical JSON with an
Ed25519 signature. It binds:

- The exact one-attempt, unstarted `CREATED` Task, workflow, immutable request
  hash, execution proposal, model profile and governance policy
- The exact staged P4.7.1 Simulation authorization ID, Research ID, Run ID,
  Batch ID, complete Simulation request/settings digest, Intent digest, and
  Region/Delay/Universe/EQUITY scope
- A unique `grant_id`, `operator_approval_id`, trusted `issuer_id` and
  `key_id`, operation `brain_simulation` only
- Strict UTC issuance/expiry (no more than 15 minutes), one simulation
  attempt, **zero additional model calls**, and Alpha submission forbidden

Any signature mismatch, disabled/unrecognized issuer, trust-key mismatch,
revoked grant, expired receipt, altered Intent/Scope or changed proposal/profile
is rejected without creating a provider, BRAIN session or authorization token.

The signed `operator_approval_id` is an identity claim, **not** independent
proof of authentic user approval. No real cost/currency ceiling can be
enforced until BRAIN platform billing and a trusted authorization service
provide corresponding evidence.

## Durable, one-shot audit reservation (not live consumption)

`reserve_consent_for_audit(...)` verifies the signature before and after
acquiring the state-root-wide cross-process reservation lock.

The canonical index under
`state/research_v1_1/operator_consent_audit/consumed.json` is published
**before** its per-task projection. It maintains reciprocal mappings for
`grant_id`, `task_id`, and `operator_approval_id`, plus a digest of the
complete projection. All three identifiers are single-use for offline audit.

A crash after the index write, missing index, partial projection, symlink,
tampered reciprocal mapping or mismatched digest is
`RECONCILIATION_REQUIRED`: there is no automatic reconstruction, retry,
second grant consumption or dispatch. Both global-stop and offline-Mock
workspace markers prohibit reservation. Audit status never upgrades
`permission_to_dispatch` above `false`.

The receipt verifier returns
`EXTERNAL_SIGNATURE_VERIFIED_NOT_AUTHORIZED`.
The durable audit status is `RESERVED_CONSENT_AUDIT_ONLY`.
Both are explicitly **non-executable**.

Read-only status CLI:

```sh
python -m alpha_research --lead deepseek --executor deepseek \\
    orchestration-consent-status <TASK_ID>
```

`orchestration-simulation-dispatch-audit` now reports the consent audit
status as additional evidence. It continues to report
`TRUSTED_LIVE_GRANT_CONSUMER_NOT_IMPLEMENTED`,
`DRIVER_OPERATION_GRANT_NOT_IMPLEMENTED`, and
`PRODUCTION_ADAPTER_DISABLED`.

There is intentionally **no CLI command to issue, trust, or consume a live
grant**, and no automatic BRAIN/paid-model/Alpha submission path.

## Offline acceptance

`tests/test_p4_operator_consent.py` covers external signature verification,
unknown/disabled/revoked issuers, expiry and budget boundaries, identity and
Intent drift, cross-task and cross-approval replay, one-shot reservation,
cross-thread races, canonical-first crash injection, corrupted/missing
projections, symlink protection and read-only negative admission.

CI covers Ubuntu/Windows Python 3.11, macOS Python 3.11/3.13, and AGY SDK
contract checks with no configured model or BRAIN credentials.

## Next production release gates

1. Implement an **external** authenticated operator-approval issuer and
   independent trust-policy lifecycle, with auditable consent/provenance,
   revocation, price/cost accounting, and private-key custody.
2. Implement a separately reviewed, atomic **live** grant consumer at the
   Gateway/Driver execution boundary, never promoting this audit index.
3. Bind one trusted grant to a live attempt journal, Task, Research ID,
   ModelCallLedger and exact Simulation Intent with crash-safe handoff.
4. Conduct explicit operator-authorized readback-only recovery drills;
   UNKNOWN must never be replayed as POST without independent proof.
5. Authorize a narrowly bounded real Simulation canary separately;
   Alpha submission remains independently gated and unsupported.

`ProductionWorkflowAdapter` and guarded Gateway writes remain disabled.
