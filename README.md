# nastu_quant: independent BRAIN research agent

This repository runs a bounded, model driven **Regular Alpha** research batch.
The model service is replaceable through configuration. The program validates
scope, Dataset, fields, expressions and gate evidence, records the proposed
hypothesis before simulation, and can recover an interrupted batch without
blindly reposting it. It has no Alpha submission or property mutation command.

The two files in `design/` are design references. The current research rules
come from `D:\quant\brain-flow\AGENTS.md`; `D:\quant\brain-flow\scripts`
is a reference for existing capabilities and evidence semantics. This is a
separate runtime in this repository and does not execute those scripts.

## Current boundary

- `run-once` completes one research batch of 1–8 expressions. `run` executes
  serial batches up to an explicit `--max-batches` limit and stops early after
  final Candidates from three different Datasets in the active case. Diagnostic
  probes are run only when the model asks for targeted missing evidence.
- Scope needs an explicit Region. Dataset can be fixed in config or selected
  from the live catalog. A configured Dataset cannot be changed by the model.
- Candidate requires complete metrics, terminal RA checks, production/self
  correlation, the active case's pairwise checks, and a clear model review.
  `PENDING` or unreadable evidence remains pending. The case contender file
  preserves pairwise conflicts for user resolution.
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
  mapped by task from the current `brain-flow/AGENTS.md`; construction advice
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

Use Python 3.11+ with `cnhkmcp` available in the chosen interpreter. Copy
[`config.example.json`](config.example.json) to a state directory **outside**
the repository, for example `D:\quant\nastu_quant_state\config\nastu_quant.json`.
Fill the explicit scope values using live supported settings. A missing Region
blocks research. The model URL is HTTPS; `json_http` accepts a JSON request
with `task`, `context`, and `instruction`, and must return
`{"result": { ... decision ... }}`. `openai_compatible` uses a chat completion
wire format and a configured model name. Implementing `complete_json(task,
context)` is the extension point for other services.

Set `NASTU_QUANT_CREDENTIAL_FILE` to an external JSON file containing the
BRAIN email and password. Set the configured model API key environment variable
when the service requires one. Secrets are not stored in this repository or
passed through the driver's JSON command stream. The runtime stores a session
cookie, batch artifacts and stop flag under the state directory.

```powershell
$env:PYTHONPATH = 'D:\quant\alpha_explore_agent\src'
$env:NASTU_QUANT_CREDENTIAL_FILE = '<external credentials JSON path>'
D:\quant-venv\Scripts\python.exe -m nastu_quant `
  --state-root D:\quant\nastu_quant_state `
  --config D:\quant\nastu_quant_state\config\nastu_quant.json `
  run-once --objective 'State the economic research question'
```

For autonomous serial batches within a fixed budget, replace the final line
with `run --objective 'State the economic research question' --max-batches 3`.
The case file retains prior batches and rejects repeat expressions.

Use `--dataset DATASET_ID` only to fix a Dataset for this batch. If config
already fixes `scope.dataset_id`, a different command line value is rejected.

## Inspect and recover

```powershell
D:\quant-venv\Scripts\python.exe -m nastu_quant --state-root D:\quant\nastu_quant_state status
D:\quant-venv\Scripts\python.exe -m nastu_quant --state-root D:\quant\nastu_quant_state stop
D:\quant-venv\Scripts\python.exe -m nastu_quant --state-root D:\quant\nastu_quant_state resume
D:\quant-venv\Scripts\python.exe -m nastu_quant --state-root D:\quant\nastu_quant_state --config D:\quant\nastu_quant_state\config\nastu_quant.json recover <batch-directory>
```

An unfinished batch blocks a fresh `run-once`. `recover` checks the saved
intent, pre-registration hash, and local result or platform account records;
it does not repost a possibly submitted expression. A draft with **no**
simulation intent can be archived while preserving its files:

```powershell
D:\quant-venv\Scripts\python.exe -m nastu_quant --state-root D:\quant\nastu_quant_state void <batch-directory> --reason 'invalid model proposal'
```

Each batch stores its context, probe plan and evidence, pre-registration,
simulation intent and raw response, read-back, gate, result and metering JSON.
`metering.json` distinguishes submission intents and confirmed responses;
model token usage is `null` unless actually measured.

`status` also lists campaign pointers. To explicitly invalidate or reinstate an
indexed evidence item, use its ID from `state/knowledge/evidence_index.json`:

```powershell
D:\quant-venv\Scripts\python.exe -m nastu_quant --state-root D:\quant\nastu_quant_state `
  evidence-validity <evidence-id> --status falsified --reason 'controlled evidence overturned this conclusion'
```

The update is evidence-preserving: the indexed source remains intact and normal
retrieval filters out any status other than `valid`.

## Knowledge maintenance

Use `knowledge list` and `knowledge show` to inspect managed notes, remedies,
and indexed evidence. Add or revise an exact operator/field note with a UTF-8
content file; replacing an existing note requires both `--replace` and a reason:

```cmd
D:\quant-venv\Scripts\python.exe -m nastu_quant --state-root D:\quant\nastu_quant_state knowledge note put --kind operator --key ts_mean --content-file D:\notes\ts_mean.md --source "operator handbook#ts_mean" --operator researcher
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

```cmd
D:\quant-venv\Scripts\python.exe -m nastu_quant --state-root D:\quant\nastu_quant_state knowledge remedy add --file D:\notes\remedy.json --operator researcher
D:\quant-venv\Scripts\python.exe -m nastu_quant --state-root D:\quant\nastu_quant_state knowledge remedy review --id R-TURN-SMOOTH --status verified --reviewer researcher --reason "batch evidence reviewed"
```

Search evidence with structured tags. `--include-history` returns only
non-valid evidence and labels every row as history-only:

```cmd
D:\quant-venv\Scripts\python.exe -m nastu_quant --state-root D:\quant\nastu_quant_state knowledge search --dataset model110 --failure-signature S_TURNOVER_HI --limit 5 --include-history
```

After a remedy has been entered as `verified` in
`state/knowledge/remedies.json`, apply it as one locked batch with:

```powershell
D:\quant-venv\Scripts\python.exe -m nastu_quant --state-root D:\quant\nastu_quant_state `
  run-once --objective 'the original objective' --diagnosis-id <diagnosis-id> --remedy-id <remedy-id>
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
$env:PYTHONPATH = 'D:\quant\alpha_explore_agent\src'
D:\quant-venv\Scripts\python.exe -m unittest discover -s tests
```
