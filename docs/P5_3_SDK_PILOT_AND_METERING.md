# P5.3 — OpenAI-compatible research SDK boundary (offline integration)

This phase integrates the **installed OpenAI Python SDK itself**, the
Task-bound signed research Grant, the P5.1 external loopback authority service,
the signed tariff inspection and durable spend quarantine in a single
non-production integration path.

**This is not an executable production research model route.**
The SDK is always constructed with an in-process MockTransport. It uses a
hard-coded synthetic API key, resolves no remote provider, cannot read model
credentials and never calls the real DeepSeek/GLM/Kimi provider. It does not
enable the existing production SDK dispatcher, BRAIN driver, Simulation or
Alpha submission.

## Control sequence

```text
Immutable Task, role, receipt and priced request plan
    |
Verify operator Grant signature and global one-use ACK
    |
Verify separately signed ledger readback and model tariff
    |
Validate bounded system/user messages and fixed ChatCompletion options
    |
Authenticated P5.1 authority: POST /v1/spend/reserve
    |  atomic: Task + Grant ACK + revocation + expiry + USD limits
    v
OpenAI Python SDK: chat.completions.create(...)
    |  SDK HTTP client forced to in-process MockTransport
    |  max_retries=0, timeout=5s, stream=False, n=1
    |  strict fixed max_tokens and model
    |
    +-- valid synthetic answer/usage --> hold RESERVED budget
    |
    +-- timeout/truncated/invalid/excess --> POST /v1/spend/quarantine
                                            |
                                            v
                                         UNKNOWN
                                   no retry, no USD released
```

The external authority calculates the **worst-case** USD reservation from
the signed maximum input/output token counts and signed rates. The caller
cannot post a desired dollar amount. All reservations are committed before
the local SDK invocation. If the SDK mock returns a successful result, the
reservation remains charged against both the per-Grant and global ceiling
because the provider has **not** supplied an independently authenticated
billing settlement. Failed/uncertain attempts transition once to UNKNOWN;
their reserved capacity is **also** never returned.

The successful mock report hashes the generated sample expression and
reports synthetic token usage and a derived estimated charge. It does not
return platform execution authority. No actual provider invoice or real
model-specific tokenizer has been consulted. The declared input count
is **not** independently verified, and OpenAI-compatible providers differ
in enforcement of token caps. These are outstanding production prerequisites.

## SDK 2.x / 3.x and transport enforcement

OpenAI Python SDK 2.x uses HTTPX; the newer 3.x uses HTTPX2.
The pilot selects the SDK's native transport family and constructs
**only** its own internal MockTransport. Never accept a caller-provided
SDK client, raw URL, provider key, proxy, transport, retry flag or
arbitrary ChatCompletion kwargs on this path.

Python SDK retries are explicitly disabled with max_retries=0, since a
lost response to a non-idempotent paid model call must not cause a duplicate.
Each attempt has a unique 32-hex attempt ID and the remote authority enforces
uniqueness globally. Reusing a prior attempt is rejected, even if the
first response was lost.

See the upstream [OpenAI HTTPX2 migration guide](https://github.com/openai/openai-python/blob/main/httpx2.md)
for transport changes. Real production support must separately verify each
provider's output-cap semantics and billing tokenization.

## Offline integration tests

```bash
python3 -m pip install -e .
python3 -m unittest discover -s tests -p 'test_p4_research_final_release.py' -v
python3 -m unittest discover -s tests -v
```

The tests use the real installed SDK, a local temporary authority server,
an isolated SQLite journal and mock HTTP transport. They cover:
valid request shape and cost preflight; zero SDK retry; usage/price math;
bad signatures and unapproved messages; missing/over-limit/truncated
completion; remote quarantine; cross-Task isolation; replay and duplicate
reservations.

For an operator-run standalone authority, refer to
[P5 local authority pilot](P5_RESEARCH_AUTHORITY_LOCAL_PILOT.md).
The service adds one API endpoint:

| Endpoint | Token | Purpose |
| --- | --- | --- |
| POST /v1/spend/quarantine | API | Permanently quarantine reserved Task/Grant/attempt on ambiguous model outcome; keeps committed spend held |

## Further work before a live Canary

- Real SDK transport remains intentionally disabled. A separate operator
  authorization and trusted service identity are required before dispatch.
- Real tokenizer usage and provider-specific hard output ceilings must be
  independently validated before computing enforceable USD upper bounds.
- Durable provider operation IDs and server-to-provider readback must drive
  settlement; a returned SDK usage object or signed *self-supplied* observation
  is not proof of the actual invoice.
- Timeouts, unknown outcomes, 429/5xx and malformed responses must not trigger
  SDK automatic retries or a new research attempt.
- Authorization revocation and operator stop checks must be in the actual
  dispatch process before every paid call.
- Only a separately approved **read-only** Canary may use real credentials;
  Simulation POST and Alpha submission require distinct approvals.

**Production remains PRODUCTION_BLOCKED.**
