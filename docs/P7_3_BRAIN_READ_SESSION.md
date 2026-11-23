# P7.3 — BRAIN scoped read-only session audit

**Delivery scope:** a complete offline Task-bound admission and readback contract. Real authenticated BRAIN session establishment remains deliberately disabled; this document is not operator approval to access an account.

## Dependencies and ownership

- `research_catalog_protocol` owns the exact read operation whitelist and Region/Delay/Universe/Instrument scope; `get_alpha_details` requires the matching Optimization source Alpha.
- `research_driver_session` verifies a domain-separated Ed25519 signed, expiry-bound session observation binding Task, research receipt, ACK, user account digest, driver build digest and allowed actions.
- `research_p5_release._check_monitor` validates the local independent Authority monitor and irreversible stop state.
- `research_p7_brain_read` combines these prior controls without importing the live BRAIN Gateway or obtaining credentials.

## Public inspection API

`inspect_p7_brain_read_session(orchestrator, task_id, receipt, ack, signed_session, session_trust, *, frames, authority_monitor, now=None)`

- Requires 1–64 bounded frames (each at most 16 KiB canonical JSON).
- Every frame must be on the verified session allowlist, match the signed scope, contain no permission-forging keys and satisfy the exact command parameters.
- Rejects duplicate frames, invalid attestation, mismatched Task/receipt/ACK, expired/revoked signatures and forged positive driver or platform permission.
- Missing monitor, a persistent STOPPED latch or a rejected read returns a distinct negative verdict. ARMED with valid frames returns `P7_BRAIN_READ_FRAME_AUDITED_NO_LIVE`, **not** a live read permission.
- Returns frame hashes and explicit `brain_read_invoked=false`, `permission_to_dispatch=false`.

`inspect_p7_brain_readback_observation(session_report, *, frame_index, observed_response)` accepts only a bounded caller-provided response envelope bound to the exact Task/frame digest/scope, HTTP 200, record count and SHA-256 digest. It never reads a response from BRAIN and classifies it as **unattested**; it does not save raw Alpha data or tokens.

## Unresolved production admission

Independent platform login and account verification, credential custody, exact research-only driver enforcement, signed readback from an authenticated BRAIN session, persistent session revocation/expiry enforcement and separately approved live read authorization remain necessary. No production or Simulation path is unlocked by P7.3.

## Deferred acceptance

`python3 -m unittest tests.test_p7_integrated_release -v` and existing P4.26/P6 catalog scope tests. Run the full offline suite only after all optimization batches, followed by explicit environment review. No live BRAIN call is part of these tests.
