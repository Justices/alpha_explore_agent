# nastu_quant: independent BRAIN research agent

This repository runs a bounded, model driven **Regular Alpha** research batch.
The model service is replaceable through configuration. The program validates
scope, Dataset, fields, expressions and gate evidence, records the proposed
hypothesis before simulation, and can recover an interrupted batch without
blindly reposting it. It has no Alpha submission or property mutation command.

The project's rules are in [`AGENTS.md`](AGENTS.md). The files in `design/`
are product requirements and historical design references. Configure
`BRAIN_FLOW_HOME` to locate the existing `brain-flow/scripts` integration;
its `AGENTS.md` is not this project's rule source.

Dataset admission reads the exact-scope `/data-sets` catalog and then the
selected `/data-sets/{dataset_id}` detail. The detail's `data` list must contain
the chosen Region, Delay and Universe before fields are admitted. The catalog
query must not use a Dataset ID as a `search` term; the live API returned HTTP
400 for that combination in the GLB/analyst69 preflight. Fixed Dataset IDs are
matched against the complete catalog locally.

The runtime reads Dataset, field and operator catalogs through the existing
`fetch_datasets.py`, `inspect_fields.py` and `fetch_brain_operators.py` entry
points. Other platform reads use the shared `brain_session.py` owner. All
simulation submissions and resumptions go through `simulate_batch.py`; this
project has no direct BRAIN simulation-create command. The agent passes the
chosen Pasteurization and `test_period` in each variant's `settings`, which the
runner merges into the effective settings used for admission and execution.

## Current boundary

- `run-once` completes one research batch of 1–8 expressions. `run` executes
  serial batches up to an explicit `--max-batches` limit and stops early after
  final Candidates from three different Datasets in the active case. Diagnostic
  probes are run only when the model asks for targeted missing evidence.
- Scope needs an explicit Region. Dataset can be fixed in config or selected
  from the live catalog. A configured Dataset cannot be changed by the model.
- Candidate requires the local configured gates plus a successful complete
  `audit_alphas.py` case audit, terminal RA checks, production/self correlation,
  active-case pairwise checks, and clear robustness/tradeoff reviews.
  `PENDING` or unreadable evidence remains pending. The case contender file
  preserves pairwise conflicts for user resolution.
- The current audit integration also applies the external script's own metric
  and correlation thresholds. This can conflict with this project's configured
  thresholds and must be reconciled before Candidate decisions are complete.
- The bounded loop cannot prove economic exhaustion automatically and has no
  user review resolution command. `auth.mode=per_batch` is rejected until an
  explicit approval flow is implemented. An exhausted or pending branch is
  reported rather than counted toward the three Dataset target. The CLI never
  submits an Alpha.
- `runs_root/backtest_cache.cache` is managed by the `BacktestCache` class. It
  stores scalable Bloom filter bit arrays and their dimensions, targeting a
  combined false-positive rate of 1e-6. Each key covers the expression,
  Dataset, Region/Universe/Delay, and complete simulation settings. A Bloom
  hit means “possibly tested”; the filter cannot tell a true hit from a false
  positive, so possible hits are conservatively skipped. Updates use an atomic
  file replacement. Startup loads the bit arrays into memory; after a simulation
  returns complete Alpha IDs, the file is atomically replaced and the in-memory
  filter is updated. A missing cache or a legacy cache is rebuilt from completed
  local batch artifacts at startup.
- Each model decision receives a deterministic knowledge pack. L1 sections are
  mapped by task from this repository's `AGENTS.md`; construction advice
  is marked advisory. L2 uses exact operator/field records only. L3 indexes
  curated `RESEARCH_MEMORY.md` priors, closed local gate evidence, and saved
  probe reports with dataset/mechanism/signature/field tags and deterministic
  BM25 ranking.
  Historical priors remain advisory. Invalidated and void evidence stays available
  only through `KnowledgeService.search_evidence(..., include_history=True)`,
  which returns history-only records carrying their validity and history marker.
  There is no vector database.
- A separate `diagnosis.json` records deterministic failure signatures and
  optional model explanations after the gate. Diagnosis cannot change a gate
  result. The remedies file starts empty; no design appendix example is treated
  as verified until it has been reviewed and entered with evidence.
- Mutating CLI operations use one OS writer lock. Campaign state is stored
  separately from the candidate ledger. Every model call is indexed in
  `state/logs/model_log.jsonl`; the original context and response are stored in
  `state/logs/model_calls/` and copied into the associated batch. Token counts
  remain `null` when the model service does not report them.
- Validation is offline. No real BRAIN simulation or model service call has
  been performed as part of repository verification.

## Configure

Use Python 3.11+ with `cnhkmcp` available in the selected interpreter. Copy
[`config.example.json`](config.example.json) to `<state-root>/config/nastu_quant.json`,
outside the repository. The default state root follows OS conventions:
`%LOCALAPPDATA%/nastu_quant` on Windows, `~/Library/Application Support/nastu_quant`
on macOS, and `${XDG_STATE_HOME:-~/.local/state}/nastu_quant` on Linux. Set
`NASTU_QUANT_HOME` to choose another location. Set `BRAIN_FLOW_HOME` to the
brain-flow repository root. Project rules always come from this repository's
root `AGENTS.md`; only advisory knowledge paths are configurable. Relative
knowledge paths are resolved from the state root. Fill the explicit
scope values using live supported settings. A missing Region blocks research.
The model URL is HTTPS; `json_http` accepts a JSON request
with `task`, `context`, and `instruction`, and must return
`{"result": { ... decision ... }}`. `openai_compatible` uses a chat completion
wire format and a configured model name. `anthropic_compatible` accepts an
Anthropic Messages base URL (the client appends `/v1/messages`), sends the key
through `x-api-key`, and reads JSON from response text blocks. A response may
contain a single fenced JSON block with surrounding explanation; ambiguous or
invalid decisions are rejected and their text is saved in the local model call
error record for diagnosis. Implementing `complete_json(task,
context)` is the extension point for other services.

Set `BRAIN_CREDENTIALS_FILE` to the external JSON file used by
`brain-flow/scripts/brain_session.py`, containing the
BRAIN email and password. Set the configured model API key environment variable
when the service requires one. Secrets are not stored in this repository or
passed through the agent's JSON command stream. BRAIN session state remains
owned by `brain_session.py`.

Set `BRAIN_PYTHON` to the absolute path of the selected environment's Python
interpreter. Install this project into that environment with `-m pip install -e .`.
The same CLI works on Windows, macOS, and Linux; only shell environment syntax
differs. For example:

```powershell
$env:BRAIN_PYTHON = '<path to the selected Python interpreter>'
$env:NASTU_QUANT_HOME = '<path to the state directory>'
$env:BRAIN_FLOW_HOME = '<path to the brain-flow repository>'
$env:BRAIN_CREDENTIALS_FILE = '<external credentials JSON path>'
& $env:BRAIN_PYTHON -m pip install -e .
function nq { & $env:BRAIN_PYTHON -m nastu_quant --state-root $env:NASTU_QUANT_HOME @args }
nq run-once --objective 'State the economic research question'
```

```sh
export BRAIN_PYTHON='<path to the selected Python interpreter>'
export NASTU_QUANT_HOME='<path to the state directory>'
export BRAIN_FLOW_HOME='<path to the brain-flow repository>'
export BRAIN_CREDENTIALS_FILE='<external credentials JSON path>'
"$BRAIN_PYTHON" -m pip install -e .
nq() { "$BRAIN_PYTHON" -m nastu_quant --state-root "$NASTU_QUANT_HOME" "$@"; }
nq run-once --objective 'State the economic research question'
```

For autonomous serial batches within a fixed budget, replace the final line
with `nq run --objective 'State the economic research question' --max-batches 3`.
The case file retains prior batches and rejects repeat expressions.

Use `--dataset DATASET_ID` only to fix a Dataset for this batch. If config
already fixes `scope.dataset_id`, a different command line value is rejected.

## Inspect and recover

```sh
nq status
nq stop
nq resume
nq recover "$BATCH_DIRECTORY"
```

Set `BATCH_DIRECTORY` to the saved batch directory before recovery.

An unfinished batch blocks a fresh `run-once`. `recover` first resumes the
saved canonical runner using its persisted simulation URL, then falls back to
read-only account reconciliation when that URL is unavailable. It never
reposts a possibly submitted expression. A draft with **no** simulation intent
can be archived while preserving its files:

Authentication and shared cookies remain owned by
`brain-flow/scripts/brain_session.py`. The project Gateway does not copy
credentials or create another BRAIN session.

```sh
nq void "$BATCH_DIRECTORY" --reason 'invalid model proposal'
```

Each batch stores its context, probe plan and evidence, pre-registration,
simulation intent and raw response, read-back, gate, result and metering JSON.
`metering.json` distinguishes submission intents and confirmed responses;
model token usage is `null` unless actually measured.

`status` also lists campaign pointers. To explicitly invalidate or reinstate an
indexed evidence item, use its ID from `state/knowledge/evidence_index.json`:

```sh
nq evidence-validity "$EVIDENCE_ID" --status falsified --reason 'controlled evidence overturned this conclusion'
```

The update is evidence-preserving: the indexed source remains intact and normal
retrieval filters out any status other than `valid`.

## Knowledge maintenance

Use `knowledge list` and `knowledge show` to inspect managed notes, remedies,
and indexed evidence. Add or revise an exact operator/field note with a UTF-8
content file; replacing an existing note requires both `--replace` and a reason:

```sh
nq knowledge note put --kind operator --key ts_mean --content-file "$NOTE_FILE" --source "operator handbook#ts_mean" --operator researcher
```

Register remedies from a JSON file. New entries always enter as
`pending_review`; promoting one to `verified` requires reviewer identity,
reason, and evidence rows containing `batch`, `alpha_id`, and `measured_change`.
`batch` must identify a local directory inside the configured `runs_root`,
using an absolute path or a path relative to that root. The batch must contain
a valid `prereg.json`/`state.json` hash pair, a `simulation_intent.json` with
`READ_BACK`, and `results.json` with that Alpha in both `readback` and `gate`.
The batch must also have either a saved simulation response containing that
Alpha or a recovery mapping that matches its pre-registered expression.
The verified review history stores the resolved batch path and pre-registration
simulation or recovery, gate, intent, and result hashes, plus a hash of the
reviewed claim.
Every remedy lookup rechecks those hashes; changed or missing evidence removes
the remedy from active lookup and marks it `evidence_invalid` in historical
lookup. The reviewer attests to `measured_change`; the service checks the local
artifact chain and presence, but does not recalculate that claim or authenticate
files against an external platform signature.
Nested preconditions can match exact `field_catalog` or `probe_observations`
facts. Changes are recorded in `state/knowledge/management_log.jsonl`; if the
audit event cannot be written, the corresponding knowledge state is rolled
back when the filesystem permits it. A failure during that rollback is
reported as an unresolved management write; the two files are not a single
crash-atomic transaction.

```sh
nq knowledge remedy add --file "$REMEDY_FILE" --operator researcher
nq knowledge remedy review --id R-TURN-SMOOTH --status verified --reviewer researcher --reason "batch evidence reviewed"
```

Search evidence with structured tags. `--include-history` returns only
non-valid evidence and labels every row as history-only:

```sh
nq knowledge search --dataset model110 --failure-signature S_TURNOVER_HI --limit 5 --include-history
```

After a remedy has been entered as `verified` in
`state/knowledge/remedies.json`, apply it as one locked batch with:

```sh
nq run-once --objective 'the original objective' --diagnosis-id "$DIAGNOSIS_ID" --remedy-id "$REMEDY_ID"
```

The command verifies the diagnosis and remedy against local evidence, fixes the
Dataset/scope/mechanism to the diagnosed lineage, and records both IDs in each
variant rationale and the new pre-registration. A lineage permits two remedy
batches; `--allow-remedy-round-overrun` is the explicit user override.
Nested remedy preconditions can match exact live catalog facts under
`field_catalog` and saved probe facts under `probe_observations`; missing facts
do not satisfy a condition. Remedy outcome writes happen after `READ_BACK` and
are idempotently retried by recovery.

## Offline checks

```powershell
& $env:BRAIN_PYTHON -m unittest discover -s tests
```

For POSIX shells, use `"$BRAIN_PYTHON" -m unittest discover -s tests`.
