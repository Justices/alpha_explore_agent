# Alpha Research

This repository runs bounded, model driven BRAIN research workflows for
Regular Alpha Campaigns, native Super Alpha construction, and optimization of
existing Regular or Super Alphas.
The model service is replaceable through configuration. The program validates
scope, Dataset, fields, expressions and gate evidence, records the proposed
hypothesis before simulation, and can recover an interrupted batch without
blindly reposting it. It has no Alpha submission or property mutation command.

The project's behavior contract is in [`AGENTS.md`](AGENTS.md), and the
approved research scope is in [`design/RESEARCH_SCOPE.md`](design/RESEARCH_SCOPE.md).
Older files in `design/` are historical design references. The brain-flow
workflow and scripts are development references only: this agent does not
import, execute, or require that repository at runtime.

Normal Alpha research does not impose PPA's operator or data-field limits.
To research Power Pool Alpha, explicitly include `PPA` (case-insensitive) or
`Power Pool Alpha` as the target in the objective. PPA expressions are checked
before simulation for at most 8 operators and 3 distinct data fields; after
read-back, the agent reports local `ppa_eligibility` using Sharpe >= 1.0.
Missing evidence remains pending. This local result is not a platform PPA
classification or a substitute for the configured Candidate gates and audit.

Dataset admission reads the exact-scope `/data-sets` catalog and then the
selected `/data-sets/{dataset_id}` detail. The detail's `data` list must contain
the chosen Region, Delay and Universe before fields are admitted. The catalog
query must not use a Dataset ID as a `search` term; the live API returned HTTP
400 for that combination in the GLB/analyst69 preflight. Fixed Dataset IDs are
matched against the complete catalog locally.

The project's own platform runtime authenticates with BRAIN, reads Dataset,
field and operator catalogs, and performs and resumes simulations. It persists
case-local catalog references, the complete effective settings and pre-backtest
receipt under the state root. A possibly submitted simulation is never reposted
automatically. Alpha submission and property mutation are not supported.

The Python package is `alpha_research`, named for the product's purpose across
all three workflows. See the [architecture](design/RESEARCH_ARCHITECTURE_V1_1.md)
for ownership and dependency boundaries. The task engine in `orchestration/`
uses an offline adapter. The existing live commands dispatch to the workflow
implementations in `workflows/` through the CLI.


## Current boundary

- `plan-regular` reads live platform settings and exact-scope Dataset catalogs
  and details to list eligible Dataset/Delay/Universe combinations for a
  Region. Repeating `--dataset` fixes a multi-Dataset request; `--category`,
  `--delay`, and `--universe` constrain discovery. The plan is saved under the
  state root in `state/research_v1_1/plans/`. This command does not simulate.
  `run-campaign --objective ... --max-batches N` executes the discovered
  combinations serially, one batch at a time, and records Campaign progress
  under `state/research_v1_1/campaign_runs/`. Regular research runs at Delay 1.
  Repeated `--dataset` arguments
  fix multiple Datasets in the supplied order. Omitting `--dataset` schedules all
  verified Datasets by ID. Each round visits one combination per Dataset before
  returning for the next combination. The batch budget applies per Dataset.
  Each effective scope uses a saved pool of up to 100 randomly
  sampled admissible fields, or all admissible fields when fewer are available.
  Dataset and field reads check account-specific exact-scope snapshots before
  fetching a complete catalog from BRAIN. Operators use one account-wide
  registry snapshot. A snapshot is reusable for seven days
  after the platform fetch. Cache misses, invalid snapshots and explicit
  refresh requests fetch again. Full catalogs are stored once in the checkout's
  `knowledge` directory, in the `datasets`, `data_fields` and
  `operator` subdirectories. The paths depend only on the catalog and scope.
  The snapshot metadata is checked against the active BRAIN account before
  reuse. A research run records the snapshot path and digest alongside the
  fields it sampled or used.
  `--ppa` explicitly enables the PPA Profile. A budget
  stop reports unvisited combinations. An interrupted draft with no submission
  evidence remains recoverable and allows unrelated research to proceed.
  Retrying the same Campaign archives its verified unsubmitted draft before
  replanning. Completed diagnostic probes can be resumed by their Campaign.
  A failed attempt is recorded and consumes one batch slot, and the Campaign
  continues within its remaining budget. Pending submissions stay recoverable.
  The platform runner rejects matching requests with unsettled POST evidence
  without blocking unrelated expressions or settings.
  Regular requests, including Regular optimization, check the account's
  completed-request Bloom cache before POST and index completed readback.
  Run `sync-backtest-cache` to index compatible Regular FASTEXPR requests from
  BRAIN IS and OS history. History entries with unverifiable or different
  effective settings are reported separately.
  Completed Regular simulations apply and verify provenance tags after readback.
  Field diagnostics receive `DIAGNOSTIC_PROBE` and their Dataset tag.
  Use `tag-alpha <alpha-id> --apply` to retry a pending tag. For production
  Alphas, `--diagnostic` adds the audited correlation name and color to an
  explicit update. Super workflows
  use the qualification audit without Bloom caching or tag plans.
  `recover-workflow --workflow regular <batch-dir>`, or the same
  command with `super` / `optimization` and a simulation-intent
  file, routes through the shared RecoveryCoordinator. `recover-task --workflow ... <task-id>`
  reconciles a blocked orchestrator execute step only when its workflow has
  persisted a recovery target. Recovery journals are durable and idempotent;
  PREPARED intents are refused, completed recoveries are not re-invoked, and
  failed/interrupted recovery is blocked for manual review. Task-level Regular
  Campaign recovery is currently limited to one batch and never starts the
  next batch.
  `plan-optimization --alpha-id ID` captures a read-only Regular or Super
  Alpha baseline and rejects missing type, settings, Region or source code.
  `plan-super --objective ... --region REGION` reads the live Super settings
  schema and the submitted ACTIVE Alpha pool without using the Regular
  Dataset catalog or starting a simulation.
  `run-super --objective ... --region REGION` constructs and simulates one
  BRAIN-native selection/combo candidate from a live ACTIVE Alpha pool.
  `run-optimization --alpha-id ID --objective ...` creates an independent
  candidate from a read-only source snapshot. A Regular source's Dataset must
  be uniquely verified against live exact-scope fields before simulation.
  Both commands leave the result pending a complete applicable Candidate audit;
  neither submits or edits an Alpha. See
  `design/RESEARCH_SCOPE.md` and
  `design/RESEARCH_STATE_V1_1.md`.
- `run-once` completes one research batch of 1–8 expressions. `run` executes
  serial batches up to an explicit `--max-batches` limit and stops early after
  final Candidates from three different Datasets in the active case. Diagnostic
  probes are run only when the model asks for targeted missing evidence.
- Scope needs an explicit Region. Dataset can be fixed in config or selected
  from the live catalog. A configured Dataset cannot be changed by the model.
- The live operator catalog is restricted to entries explicitly available to
  Regular Alphas. Unambiguous live signatures are checked before simulation;
  an unclear signature is recorded as unverified rather than guessed.
- Candidate requires the local configured gates, complete local audit and
  case evidence, terminal platform checks, production/self correlation,
  active-case pairwise checks, and clear robustness/tradeoff reviews. Numeric
  audit evidence is evaluated against this project's configuration. Missing,
  unreadable, changed or unrecognized evidence remains pending. The case contender file preserves
  pairwise conflicts for user resolution.
- When readable RA checks have no failures, including while some are pending,
  a four-year RA-gated local self-correlation precheck followed by PC/SC correlation query compares same-region OS Alphas
  excluding Power Pool Alphas. A breach or unavailable comparison prevents
  platform correlation requests. Terminal platform correlation checks are
  reused; pending endpoint responses receive bounded retries. Retained case
  contenders have their scope, RA state and local prefilter checked again before
  a new batch's pairwise comparisons. A retained Candidate is first marked
  pending on disk, then must pass a fresh complete audit bound to its original
  pre-registration and simulation receipt before it is counted again. Case-pair
  correlations use cumulative PnL changes over matching date intervals, with
  input snapshots and hashes saved in the case state. OS pools and PnL are reused
  only within one batch or audit;
  each new batch and audit starts with fresh platform reads. A missing
  risk-neutralized block is recorded as absent; an
  unreadable or incomplete block remains pending.
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
  configured `RESEARCH_MEMORY.md` priors, closed local gate evidence, and saved
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

Use Python 3.11+ and install this project with its declared dependencies. Copy
[`config.example.json`](config.example.json) to `<state-root>/config/nastu_quant.json`,
outside the repository. The default state root follows OS conventions:
`%LOCALAPPDATA%/nastu_quant` on Windows, `~/Library/Application Support/nastu_quant`
on macOS, and `${XDG_STATE_HOME:-~/.local/state}/nastu_quant` on Linux. Set
`ALPHA_RESEARCH_HOME` to choose another location. Existing installations can
continue to use `NASTU_QUANT_HOME`. The default
directory and config filename retain their existing names for state and
recovery compatibility. Project rules always come from
this repository's root `AGENTS.md`; only advisory knowledge paths are
configurable. Relative
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

Set `BRAIN_CREDENTIALS_FILE` to an external JSON file containing the BRAIN
email and password, either at the top level or inside a `credentials` object.
Set the configured model API key environment variable when the service requires
one. The agent uses its own in-memory BRAIN session; it does not print or store
credential values.

The example model router uses the locally installed Codex CLI by default.
`attribute_results` and `diagnose_failures` use AGY CLI. Set an entry in
`model.routes` to a name from `model.backends` to change that decision node.
The example includes Claude CLI as another selectable backend. CLI backends
use the tool's existing local login and accept optional `executable`,
`model`, and `timeout_seconds` settings. HTTP backends can be added under
`model.backends` using the same provider settings documented above.
An existing single-provider `model` configuration continues to work.

Codex CLI research decisions share a saved Codex thread within a Regular
Campaign, bounded Regular run, Super run, or Optimization run. Regular
recovery uses the research identity saved with the batch and resumes the
recorded thread by ID. Thread bindings are stored under
`<state-root>/state/codex_threads`. The Codex CLI retains the conversation
history in its own session storage.

The Workbench is a standalone Python service. For a single operator on a
Linux host, run the following from the checkout under a dedicated service
account (for example, with systemd managing the process):

```sh
python3 -m pip install -e .
python3 workbench/serve.py --port 8766 --state-root /var/lib/alpha-research
```

Give the service account write access to the checkout's `knowledge`
directory. Keep the state directory on persistent storage and place the configuration
in `config/nastu_quant.json` under that directory. Set model key environment
variables for the service account. If you keep a CLI backend, install and
authenticate it as that account. Preserve Codex CLI session storage as well as
the research state if you need to resume Codex threads. For a service that
does not depend on desktop logins, set the router's default and every route
to configured HTTPS backends.

The current Workbench is for one operator. It binds to loopback, checks the
local Host and Origin, and keeps browser BRAIN credentials in memory for up to
24 hours. To use it from another machine, forward the port with
`ssh -N -L 8766:127.0.0.1:8766 user@research-host`, then open
`http://127.0.0.1:8766/` and enter BRAIN credentials in the Workbench.
An unattended CLI job can instead use the service account's
`BRAIN_CREDENTIALS_FILE`. A shared web deployment needs an authenticated HTTPS
application, a durable task queue with a worker, and user/account isolation.

Open **研究任务 → 执行日志** for the latest web launch's live progress.
The per-launch timeline is also saved to
`<state-root>/state/logs/workbench/<launch-id>.jsonl`.
The same page shows the shared catalog snapshot directory. Catalog events
record the exact scope, cache hit or refresh reason, full record count,
fetch time, and file path. The field-pool event separately records how many
fields were eligible and how many were selected for research.
For a standalone CLI run, set `ALPHA_RESEARCH_PROGRESS_FILE` to an absolute
JSONL path in an existing writable directory to capture these events.
Restarting the Workbench resets the displayed launch and the browser credential
session. Use the saved logs and Campaign checkpoints to reconcile interrupted
work before resuming it.

Install and run the project with the Python interpreter selected for this agent.
For Windows Command Prompt:

```cmd
set ALPHA_RESEARCH_HOME=<path to the state directory>
set BRAIN_CREDENTIALS_FILE=<external credentials JSON path>
python -m pip install -e .
python -m alpha_research --state-root %ALPHA_RESEARCH_HOME% run-once --objective "State the economic research question"
```

On macOS or Linux:

```sh
export ALPHA_RESEARCH_HOME='<path to the state directory>'
export BRAIN_CREDENTIALS_FILE='<external credentials JSON path>'
python3 -m pip install -e .
python3 -m alpha_research --state-root "$ALPHA_RESEARCH_HOME" run-once --objective 'State the economic research question'
```

The examples below use `nq` as shorthand for the corresponding
`python -m alpha_research --state-root <state-directory>` command.

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
nq recover-research "$SIMULATION_INTENT_PATH"
```

Set `BATCH_DIRECTORY` to the saved batch directory before Regular batch recovery. Set `SIMULATION_INTENT_PATH` to the saved V1.1 Super or Optimization `*_simulation_intent.json` file for `recover-research`. The V1.1 command uses the existing platform `resume_simulation_batch` ledger and never invokes a new simulation POST; if the platform cannot prove and resume the recorded operation, the intent remains unresolved.
`stop` is serialized with the start of a simulation POST. It waits for an
already-started POST to finish before confirming the stop; it does not cancel
an existing simulation. If it cannot acquire the submission lock within 60
seconds, it returns an error instead of confirming a stop.

An unfinished batch blocks a fresh `run-once`. `recover` first resumes the
local runner using its persisted simulation URL. If that URL is unavailable,
account matches are recorded as possible matches and remain unresolved because
their batch ownership cannot be established. It never reposts a possibly
submitted expression. A batch recorded as stopped before its first POST can be
started once by `recover` after an explicit `resume`, using its saved intent and
locked research evidence. A draft with **no** simulation intent can be archived
while preserving its files:

Authentication and session cookies are managed by this project's own runtime.
Credentials stay in the selected external file.

```sh
nq void "$BATCH_DIRECTORY" --reason 'invalid model proposal'
```

Each batch stores its context, probe plan and evidence, pre-registration,
simulation intent and raw response, read-back, gate, result and metering JSON.
`metering.json` distinguishes submission intents and confirmed responses;
model token usage is `null` unless actually measured.

`status` also lists legacy and V1.1 Campaigns, Super runs, Optimization source snapshots, candidate branches, and their simulation-intent/readback/audit/report references. To explicitly invalidate or reinstate an
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

```sh
python -m unittest discover -s tests
```


## Candidate and Preferred

Candidate and Preferred are separate lifecycle stages. The existing Candidate
gate remains authoritative and requires the configured Sharpe precondition,
terminal RA checks with zero failures, production/self correlation strictly
below 0.70, pairwise correlation strictly below 0.40, and all existing
evidence gates. Correlation requests are blocked while RA checks are pending,
unreadable, or failed.

Only a fully reconciled `CANDIDATE` is evaluated by the second-stage
`preferred-v1` selector. Its configured defaults are Sharpe >= 1.25,
Fitness >= 1.0, Turnover in [0.01, 0.70], maximum weight <= 0.10, and
`LOW_SUB_UNIVERSE_SHARPE` PASS. Missing metrics/checks remain
`PENDING_PREFERRED`; failed criteria become `NOT_PREFERRED`. Drawdown and
Coverage are recorded for analysis but have no threshold until explicitly
approved. Preferred selection does not submit Alpha.


### Shared Candidate Qualification and Pools

Regular, BRAIN-native Super, and Alpha Optimization now use
`CandidateQualificationService` as their common Candidate/Preferred
lifecycle. Regular passes its reconciled canonical audit evidence; Super and
Optimization use read-only platform detail, RA, local self-correlation, and
production/self-correlation calls through the existing gateway.

The service checks RA first. If RA is failed, pending, missing or unreadable,
it does not request production/self correlations. Only after RA is terminal
PASS does it run the existing RA-gated local self-correlation precheck followed by PC/SC correlation query and then query
PC/SC. PC and SC must both be strictly below 0.70. Candidate qualification
also retains the configured metric gates and platform-check completeness.
Only final `CANDIDATE` records can become `PREFERRED`.

Every workflow candidate receives a versioned Candidate Pool record and a
corresponding Preferred Pool evaluation record, with stable IDs, workflow,
source/candidate lineage, metrics, failure/pending reasons and evidence refs.
The `status` command lists both pools. These paths only read BRAIN Alpha
evidence; they do not submit or modify Alphas.


### Incremental Candidate pool maintenance

Use `nq status` to inspect the deduplicated global Candidate/Preferred pool and
all per-Workflow evaluation ledgers. Use `nq rebuild-pools` to reconstruct the
derived global index solely from saved versioned evaluation records. Rebuilding
does not contact BRAIN, resimulate or submit an Alpha.

Candidate records are keyed by workflow + Alpha ID + candidate reference, so
different Campaigns and Optimization branches keep distinct research/audit
lineage. Their global views deduplicate by Alpha ID, selecting the latest
evaluation rather than an older PASS. Re-evaluations with identical evidence
are idempotent; changed records archive previous revisions under
`state/research_v1_1/evaluation_history/` before replacement. Reuse requires
a matching current evidence and policy hash, never just matching Alpha ID.
