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

## Offline checks

```powershell
$env:PYTHONPATH = 'D:\quant\alpha_explore_agent\src'
D:\quant-venv\Scripts\python.exe -m unittest discover -s tests
```
