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
For the current architecture, implementation status, and design-document index,
start with [`design/CURRENT_DESIGN.md`](design/CURRENT_DESIGN.md).
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


## System architecture, operations and release boundaries

The project is organized by **workflow, model and platform integrations,
evidence, orchestration, budgeting, recovery and authorization**, rather
than historical delivery milestones. Use these maintained entrypoints:

| Topic | Current document |
| --- | --- |
| Architecture and workflow ownership | [System Design](design/CURRENT_DESIGN.md) |
| Package responsibilities | [Component Architecture](design/RESEARCH_ARCHITECTURE_V1_1.md) |
| Read-only Task audit, local Authority and operational safety | [Operations](docs/OPERATIONS.md) |
| CI, audit evidence and production blockers | [Release Acceptance](docs/RELEASE_ACCEPTANCE.md) |
| Historical implementation records | [Archive](docs/archive/README.md) |

`orchestration-research-status <TASK_ID>` reports preparation,
attempts, recovery and blockers without dispatch. The read-only
`orchestration-release-gate <TASK_ID>` correlates Provider billing,
BRAIN scoped-read observations, Canary telemetry and external requirements.
The local Authority and SDK MockTransport are isolation references,
not externally attested production infrastructure.

The current research control plane remains **`P7_PRODUCTION_BLOCKED`**:
a signed local report, matching caller invoice, or green offline test
never authorizes live BRAIN read/Simulation, paid model dispatch or Alpha
submission. See [Release Acceptance](docs/RELEASE_ACCEPTANCE.md) for
independently owned deployment and operator approval prerequisites.

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

## Research component boundaries

The Regular workflow's catalog admission, model decisions, field plans,
Candidate audit and local Case evidence are independent modules. The existing
workflow coordinator still owns Simulation intents and recovery, and the
MainOrchestrator remains separate from the fixture-only research-round runner.
See [Research Refactor Boundaries](design/RESEARCH_REFACTOR_BOUNDARIES.md)
for the code ownership map, safety boundaries and regression coverage.

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
Model/Agent selection uses the single bundled registry at
[`alpha_research/agents/model_registry.json`](alpha_research/agents/model_registry.json).
The business config no longer embeds model backend definitions; research roles
and task bindings are defined only by the bundled
[`role_profile.json`](alpha_research/agents/role_profile.json).
Start with `python -m alpha_research models list`; use
`--lead <registry-id> --executor <registry-id>` to select model providers.
Set the appropriate API key environment variable for HTTP/SDK providers.
The CLI provider family supports Codex and Claude Code; AGY uses its SDK;
DeepSeek, GLM, Kimi and other LLM services use the appropriate HTTP adapter.
The orchestrator, not the model provider, owns all BRAIN tool permissions and
Simulation Intent recovery. See
[Model/Agent Registry](design/MODEL_AGENT_REGISTRY.md) and
[Model Runtime Governance](design/MODEL_RUNTIME_GOVERNANCE.md).

Set `BRAIN_CREDENTIALS_FILE` to an external JSON file containing the BRAIN
email and password, either at the top level or inside a `credentials` object.
Set the configured model API key environment variable when the service requires
one. The agent uses its own in-memory BRAIN session; it does not print or store
credential values.

The canonical runtime is the role-based model router; the old
`provider=router`, single-provider startup configuration, and AGY CLI
compatibility have been retired. Use these local-only commands to check a
selected combination without running a model or BRAIN Simulation:

```powershell
python -m alpha_research models list
python -m alpha_research --lead codex --executor glm models inspect
python -m alpha_research --lead kimi --executor claude-code models doctor
```

Model decisions are indexed under `state/logs/model_calls/`. The JSONL audit
log is append-only (one event for each state transition), and the per-call
`.record.json` is the current-state record; `model_call_manifest.json`
contains references to the verified batch evidence. Research-role model
identity and budget are pinned for recovery. Codex CLI threads use the
research ID and role to isolate retained contexts; ephemeral CLI sessions
do not persist threads.

A real model smoke test requires an explicit `--allow-model-call` flag.
It never authorizes BRAIN Simulation or Alpha submission.

### Local readiness inspection (no external calls)

Before choosing a live Regular/Super/Optimization command, verify the local
research configuration, model role IDs, registered backend prerequisites,
bounded model-attempt budget and external BRAIN credential-file format:

```sh
python -m alpha_research --state-root "<state-root>" \
  --lead glm --executor kimi preflight --region CHN
```

`READY_LOCAL` is **not** a login or API contract check, and does not grant
Simulation permission. Follow the staged acceptance checklist in
[Current operations and authorization boundaries](docs/OPERATIONS.md).
BRAIN read-only planning commands (`plan-regular`, `plan-super`,
`plan-optimization`) do not require a model registry or Lead/Executor
selection; they do require authorized BRAIN access when executed.
Actual research with registered live backends requires an explicit
`model_governance` attempt/token budget and `failover=manual`.

The Workbench is a standalone Python service. For a single operator on a
Linux host, run the following from the checkout under a dedicated service
account (for example, with systemd managing the process):

```sh
python3 -m pip install -e .
python3 workbench/serve.py --port 8766 --state-root /var/lib/alpha-research
```

For a Windows workstation, run the service from **Command Prompt** in the
repository root. Use the same Python interpreter and persistent State Root as
the CLI:

```cmd
cd /d D:\quant\alpha_explore_agent
D:\quant-venv\Scripts\python.exe -m pip install -e .
D:\quant-venv\Scripts\python.exe workbench\serve.py --port 8765 --state-root D:\quant\alpha_explore_agent\.alpha-runtime
```

Keep this terminal open and visit `http://127.0.0.1:8765/` in the browser.
The server binds to loopback only; stop it with `Ctrl+C`. Starting the server
does not call BRAIN or invoke a model. BRAIN credentials are entered through
the Workbench and kept in the server process memory for up to 24 hours.
Use the separate preflight action to inspect a scope; starting an actual
research workflow requires a second, explicit confirmation.

Give the service account write access to the checkout's `knowledge`
directory. Keep the state directory on persistent storage and place the configuration
in `config/nastu_quant.json` under that directory. Set model key environment
variables for the service account. If you keep a CLI backend, install and
authenticate it as that account. Preserve Codex CLI session storage as well as
the research state if you need to resume Codex threads. For a service that
does not depend on desktop logins, register the intended HTTPS LLM API
backends and select their IDs through the `--lead` and `--executor` flags.

The Workbench loads the same model registry as the CLI and provides Lead and
Executor selection for all three live research workflows:

- **Regular** launches multi-Dataset Campaigns. The current Regular
  executor is restricted to Delay 1; Dataset, Category, Universe, batch
  budget, and PPA Profile remain optional constraints.
- **Super** launches the BRAIN-native Selection / Combo workflow with a
  bounded 1–20 iteration budget.
- **Optimization** launches an independent candidate branch from a required
  source Alpha ID, also with a bounded 1–20 iteration budget.

Each workflow form also provides a **preflight**. Regular preflight calls
`set_case` to establish the BRAIN case context before checking the requested
scope and Dataset combinations; the UI asks for confirmation and this step
does not invoke Simulation. Super preflight inspects the platform-native pool
and settings. Optimization preflight captures the source Alpha snapshot.
None of these preflights submits an Alpha or invokes Simulation. Live research
requires a separate explicit confirmation in the UI.

The local server verifies model availability and governance before launching
a child process. Regular Campaigns can be resumed with their original model
selection. Super and Optimization expose explicit Simulation Intent recovery;
recovery reconciles platform receipts and never automatically reposts an
unknown Simulation. The project UI never automatically submits an Alpha. The
server never exposes API key values through the registry listing and does not
select a provider implicitly. If the registry
is not bundled, pass `--registry /path/to/model_registry.json` to
`workbench/serve.py`.

When running in project mode, hand-entered research cases and Alpha evidence
are stored under `<state-root>/state/workbench/records.json`; the standalone
HTML file continues to use browser storage. JSON import/export remains
available in both modes.

The current Workbench is for one operator. It binds to loopback, checks the
local Host and Origin, and keeps browser BRAIN credentials in memory for up to
24 hours. To use it from another machine, forward the port with
`ssh -N -L 8766:127.0.0.1:8766 user@research-host`, then open
`http://127.0.0.1:8766/` and enter BRAIN credentials in the Workbench.
An unattended CLI job can instead use the service account's
`BRAIN_CREDENTIALS_FILE`. A shared web deployment needs an authenticated HTTPS
application, a durable task queue with a worker, and user/account isolation.

Open **研究任务 → 执行日志** for the latest web launch's live progress.
Each launch has an atomic `<launch-id>.launch.json` manifest beside its
`.jsonl` timeline; the manifest stores the selected workflow, model IDs,
non-secret arguments, terminal result, and timestamps. The Workbench exposes
the latest 50 manifests as launch history. On service restart, a manifest
left in `running` is restored as `execution_unknown`; new launches are
blocked until the operator confirms that the old Agent child process has
exited. This acknowledgement never retries a workflow or Simulation.
The same page shows the shared catalog snapshot directory. Catalog events
record the exact scope, cache hit or refresh reason, full record count,
fetch time, and file path. The field-pool event separately records how many
fields were eligible and how many were selected for research.

The **审核** view includes live Candidate and Preferred evaluation
records read from the Agent state directory, including failed and pending
evaluations such as `RA_FAILED`, `RA_UNAVAILABLE`, and unresolved Preferred
reviews. The view displays the qualification policy fingerprint and
distinguishes a stale policy from a record that has no policy binding. Only
candidates whose current RA and correlation gates pass are promoted into the
Candidate pool; a pending evaluation remains visible for audit but is not
counted as a Candidate. For a standalone CLI run, set
`ALPHA_RESEARCH_PROGRESS_FILE` to an absolute JSONL path in an existing
writable directory to capture these events.
Restarting the Workbench resets the displayed launch and the browser credential
session. Use the saved logs and Campaign checkpoints to reconcile interrupted
work before resuming it.

Install and run the project with the Python interpreter selected for this agent.
For Windows Command Prompt:

```cmd
set ALPHA_RESEARCH_HOME=<path to the state directory>
set BRAIN_CREDENTIALS_FILE=<external credentials JSON path>
python -m pip install -e .
python -m alpha_research --state-root %ALPHA_RESEARCH_HOME% --lead glm --executor kimi run-once --objective "State the economic research question"
```

On macOS or Linux:

```sh
export ALPHA_RESEARCH_HOME='<path to the state directory>'
export BRAIN_CREDENTIALS_FILE='<external credentials JSON path>'
python3 -m pip install -e .
python3 -m alpha_research --state-root "$ALPHA_RESEARCH_HOME" --lead glm --executor kimi run-once --objective 'State the economic research question'
```

The examples below use `nq` as shorthand for the corresponding
`python -m alpha_research --state-root <state-directory>` command.
**Research commands below are live BRAIN operations** and can incur paid
model usage or post a Simulation; execute them only with explicit approval.
`glm` and `kimi` are illustrative registered IDs, not automatically
selected defaults. Check `preflight` and `models doctor` first.

For autonomous serial batches within a fixed budget, replace the final line
with `nq --lead glm --executor kimi run --objective 'State the economic research question' --max-batches 3`.
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
nq --lead glm --executor kimi run-once --objective 'the original objective' --diagnosis-id "$DIAGNOSIS_ID" --remedy-id "$REMEDY_ID"
```

The command verifies the diagnosis and remedy against local evidence, fixes the
Dataset/scope/mechanism to the diagnosed lineage, and records both IDs in each
variant rationale and the new pre-registration. A lineage permits two remedy
batches; `--allow-remedy-round-overrun` is the explicit user override.
Nested remedy preconditions can match exact live catalog facts under
`field_catalog` and saved probe facts under `probe_observations`; missing facts
do not satisfy a condition. Remedy outcome writes happen after `READ_BACK` and
are idempotently retried by recovery.

## Unified orchestration: offline preparation and domain handoff

Before an explicitly reviewed production integration, a task and its selected
model identities can be prepared without opening a BRAIN session or calling a
model. Both roles must be selected from the model registry. If the
default `<state-root>/config/nastu_quant.json` does not exist, **offline
preparation and inspection only** use built-in research defaults plus a
conservative model governance policy (`max_calls=1`,
`max_reported_tokens=4096`, `failover=manual`). This policy is only a
fingerprinted planning limit; it does not authorize model calls. If a user
configuration exists (or is explicitly selected via `--config`), it must
include valid `model_governance`. Missing explicit files are errors.

```sh
nq --state-root "$STATE_ROOT" --lead deepseek --executor deepseek \
  orchestration-prepare --workflow regular --region CHN \
  --objective "offline preparation only"

nq --state-root "$STATE_ROOT" --lead deepseek --executor deepseek \
  orchestration-handoff <TASK_ID>
```

After preparing the Task ID, exercise its persisted four-stage control plane
with a built-in **synthetic fixture** (no domain Dispatch, no model, no
Gateway and no Candidate Pool mutations):

```sh
nq --state-root "$STATE_ROOT" --lead deepseek --executor deepseek \
  orchestration-run <TASK_ID> --mode mock

nq --state-root "$STATE_ROOT" orchestration-status <TASK_ID>
```

macOS full-suite verification (particularly Homebrew Python 3.13) should
use a canonical, writable temp directory. The macOS CI job runs Python
3.11 and 3.13 with this same `TMPDIR` normalization:

```sh
mkdir -p "$HOME/.cache/alpha-research-tmp"
export TMPDIR="$(cd "$HOME/.cache/alpha-research-tmp" && pwd -P)/"
python3 -m unittest discover -s tests -v
```

This avoids the macOS `/var` versus `/private/var` temporary-path alias
in local test fixtures; it does not relax path/symlink security checks.
The dedicated mock workspace must be disposable; do not execute any
`orchestration-domain-mock` task in the real research evidence root.

For a **new** prepared task, run the domain-level offline integration
through the actual Regular Campaign scheduler or Super/Optimization workflow
code, using scripted mock catalog data, batch results, model decisions and
simulation responses. Keep this separate from the synthetic control-plane
dry run (both consume the task).

```sh
# Use a new, disposable state-root; do NOT use the default research root.
STATE_ROOT="$(mktemp -d)"
# Prepare a NEW task for each experiment; Regular requires Delay 1.
nq --state-root "$STATE_ROOT" --lead deepseek --executor deepseek \
  orchestration-prepare --workflow regular --region CHN \
  --objective "offline Regular domain mock"

# Use the task_id from the immediately preceding result:
nq --state-root "$STATE_ROOT" --lead deepseek --executor deepseek \
  orchestration-domain-mock <TASK_ID> --mode mock

# To test an unknown simulation outcome, prepare ANOTHER new task:
nq --state-root "$STATE_ROOT" --lead deepseek --executor deepseek \
  orchestration-domain-mock <NEW_TASK_ID> --mode mock \
  --fault lost-simulation-response
```

The mock command verifies the saved immutable model identity and proposal.
Regular uses actual catalog scope discovery and Campaign persistence with a
scripted `run_once` batch, **not** a real ResearchAgent generation loop.
Super/Optimization run their actual domain functions and write local mock
Simulation Intents and research records. The mock Gateway/Model never reach
external providers, and the output explicitly labels this as
`offline_domain_mock`. Its records are testing artifacts, **not live Alpha
evidence**. Always use a dedicated disposable `--state-root` for tests, not
your production evidence directory. Unknown mock outcomes remain `BLOCKED`
with reserved execution budget and cannot be retried automatically.

P3 mock isolation is **enforced in code**: only a dedicated, initially
evidence-free, non-default state root may enter domain Mock mode; a persistent
`OFFLINE_DOMAIN_MOCK_ONLY` workspace marker prevents live workflow commands
from operating on that root. Every research record written during a CLI
domain Mock carries `evidence_origin=OFFLINE_DOMAIN_MOCK`. CandidatePool
omits these records from its index and reuse lookups, even when a user manually
points a pool reader at the mock root.

P3 also defines an externally signed execution-receipt **verification**
contract (separate from review-only approvals). Inspection and optional
single-use audit reservation are available as follows:

```sh
nq --state-root "$STATE_ROOT" --lead deepseek --executor deepseek \
  orchestration-authorization-inspect <TASK_ID> --receipt /absolute/path/receipt.json

# Optional; consumes the receipt ID for AUDIT ONLY, never execution:
nq --state-root "$STATE_ROOT" --lead deepseek --executor deepseek \
  orchestration-authorization-reserve <TASK_ID> --receipt /absolute/path/receipt.json
```

These commands require an independently issued HMAC-SHA256 receipt and the
`ALPHA_RESEARCH_EXECUTION_VERIFY_KEY` verifier environment variable; this
application does not sign/issue receipts. A valid inspection returns
`INSPECTED_NOT_EXECUTABLE`; reservation returns
`RESERVED_INSPECTION_ONLY`. Both always report
`permission_to_dispatch=false`. Neither command grants model, Simulation,
readback or submission privileges, and no production adapter is enabled.

Inspect admission blockers for a newly prepared task without making calls:

```sh
nq --state-root "$STATE_ROOT" --lead deepseek --executor deepseek \
  orchestration-admission-audit <TASK_ID>
```

For the current regression and production blocker matrix, see
[Release Acceptance](docs/RELEASE_ACCEPTANCE.md). A local offline success
never authorizes real BRAIN execution.

The audit result is always `NOT_AUTHORIZED`. It checks proposal/identity,
budget, config provenance, review receipt state and mock isolation, but cannot
create execution permission or a provider token. The production adapter
remains fail-closed. See [Release Acceptance](docs/RELEASE_ACCEPTANCE.md) for the contract
and remaining external prerequisites.

This dry run is **not** a BRAIN research Simulation or a domain end-to-end
Mock run. It validates the prepared immutable execution identity and proposal,
then advances Validate / Execute / Qualify / Collect using synthetic checkpoints.
Only `CREATED` tasks are admissible; the prepared task becomes `COMPLETED`
and cannot later be turned into a live run. Create a separate task for
further tests or eventual independently authorized execution. The output
explicitly records `domain_dispatch_invoked=false` and `brain_invoked=false`.

For Optimization, add `--workflow optimization --alpha-id <SOURCE_ALPHA_ID>`.
These commands only write a `CREATED` task, an
`execution_proposals/` record and an `execution_identities/` record, or
read existing records. No model client/Gateway is constructed and no
Simulation, tag mutation or Alpha submission occurs.

The domain execution functions now report their true `research_id` into the
orchestrator's durable execution context, before external requests. The
read-only handoff inspection reports the **existing**
`ModelCallLedger` state for that Research ID and preserves unresolved
in-flight attempts. A Regular Campaign may reuse a deterministic Research ID
across tasks; that budget is governed per Research ID, not per task.

Use the read-only P2 recovery preflight to inspect a blocked task
without repeating any Simulation or querying BRAIN:

```sh
nq --state-root "$STATE_ROOT" --lead deepseek --executor deepseek \
  orchestration-recovery-check <TASK_ID>
```

It distinguishes a missing Intent, an unsubmitted `PREPARED` Intent,
unresolved model calls, and recoverable platform reconciliation. All outcomes
have `automatic_replay_allowed=false`. This command does **not** issue a
RecoveryCoordinator receipt or settle an unknown model call.

P2 controlled integration can execute the same control-plane steps only
with a project `MockWorkflowAdapter` backed by offline Mock Gateway and
Mock Model. A normal `ProductionWorkflowAdapter` still rejects every live
execution before budget reservation; no CLI command enables a real dispatch.

**This is not a live execution command.** `ProductionWorkflowAdapter`
continues to reject unapproved research at validation. CLI-owned Gateway
authentication, the `state/agent.lock` process lock, domain Simulation
Intent and verified RecoveryCoordinator receipt remain mandatory for any
future separately authorized live integration.

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


### One-shot BRAIN Canary (one fixed expression, explicit authorization)

This is a legacy-domain real-API integration test, **not** approval for the new P7 production orchestrator. It never invokes a model, generates more expressions, submits an Alpha, or auto-PATCHes Alpha tags. The source of truth is the exact completed dataset plan. The prepare command authenticates to BRAIN, validates the complete live field/operator catalogs and effective Simulation schema, and writes a durable pre-registration. It sends no POST.

```powershell
$stateRoot = Join-Path $env:LOCALAPPDATA "alpha-research-live"
python -m alpha_research --state-root $stateRoot canary-prepare --plan-id c619095d74394a0bb3d89d24910963c6
python -m alpha_research --state-root $stateRoot canary-execute --plan-id c619095d74394a0bb3d89d24910963c6 --allow-one-simulation
python -m alpha_research --state-root $stateRoot canary-status --plan-id c619095d74394a0bb3d89d24910963c6
```

The first actual Simulation dispatch permanently consumes the local one-shot reservation before attempting the network request. A timeout or lost response must be reconciled with the durable platform ledger; do not rerun the same POST or reset the reservation. Standard backend Simulation submission-gate and duplicate-cache safeguards remain active. The Canary must not automatically mutate Alpha tags. Only one expression, rank(analyst_revision_ratio_to_close_ebitda_q1), is accepted. Fixed initial settings are INDUSTRY neutralization, decay 4, truncation 0.08, pasteurization ON, unit handling VERIFY, NaN OFF, full test period, maxTrade OFF. Every setting must be verified against the current live BRAIN schema; missing or incompatible settings block, rather than guessing substitutes. This is a real BRAIN simulation and may incur usage; it does not confer Candidate status.


#### Offline post-canary evidence and metric review

After the one-shot canary has finished and saved a complete result, use `python -m alpha_research --state-root <same-root> canary-review --plan-id <original-plan-id>`. This command reads **only local state**: the original exact Dataset plan, immutable canary registration, platform request/admission receipt/POST marker/progress/output hashes, Alpha readback expression and settings, and the existing local metric gate configuration. It never constructs a Gateway, runs a model, POSTs another Simulation, submits an Alpha, or modifies its tags. A completed and verified result is classified `BELOW_THRESHOLD` when any Sharpe/Fitness/Turnover metric is below the research policy, otherwise `FURTHER_AUDIT_REQUIRED`. This is a **preliminary metric screen only**, not an official Candidate evaluation: full RA, correlation, reliability and lineage audits remain separate. The saved `state/canary/<plan-id>_review.json` is immutable and bound to the output digest and config policy. Tampered or incomplete local evidence is rejected, not inferred.

Canary review readback caveat: BRAIN may omit `testPeriod` from `GET /alphas/{id}` despite its presence in the original Simulation POST payload. For the pinned `P0Y0M` Canary only, the audit marks this as `settings_readback_verification.status=PARTIAL_READBACK` with `not_echoed=["testPeriod"]`, provided the exact original POST request fingerprint and immutable evidence receipts pass first. This **does not prove the platform applied the missing parameter**. Explicit zero-duration aliases are accepted and labeled as normalized. All other missing or mismatched requested settings block the audit with fixed setting-key names; no re-simulation, model or Alpha submission occurs.

#### Offline failed-canary triage

After a successful `canary-review` yields `BELOW_THRESHOLD`, run `python -m alpha_research --state-root <same-root> canary-diagnose --plan-id <plan-id>`. This command reads the immutable local review and hash-matched Simulation output; projects the existing BRAIN RA check names/results, derives the repository's existing failure signatures, and records **explicitly unproven research hypotheses** in `state/canary/<plan-id>_diagnosis.json`. It does not contact BRAIN, run another Simulation, call an LLM, execute correlation checks or qualify an Alpha. A missing, inconsistent, or tampered audit/RA result blocks rather than producing false confidence. The generated triage does not create a new simulation authorization.


#### Round-two offline Canary research design

After `canary-review` and `canary-diagnose` have completed, run `python -m alpha_research --state-root <same-root> canary-design --plan-id <original-plan-id>`. The command never contacts BRAIN or a model: it uses the immutable prior audit and diagnosis, the pinned original account's `knowledge/data_fields/USA/1/TOP3000/analyst10.json` and `knowledge/operator/operators.json` with exact case-reference SHA-256 and TTL validation. The expression validator checks real REGULAR operator membership, full dataset field ID/type and **unambiguous live signatures**. Candidates with unknown operators or missing signatures are `STATIC_BLOCKED`, not silently admitted. Proposals only express unverified hypotheses for smoothing, relative-surprise and persistence; concentration remains explicitly unresolved. The immutable `state/canary/<plan-id>_round2_design.json` is bound to the full input hashes and may be read for manual research review, **never submitted directly to Simulation**. No Alpha or Candidate is created and no additional Simulation authorization is granted.

Round-two catalog path: `canary-design` reads pinned `*.ref.json` files from `<state-root>/state/platform/cases/canary_<plan-id>/sessions/single_<plan-id>/fields/...` and `.../catalogs/operators.ref.json`, where `PlatformRuntime.set_case` actually writes them. An untrusted case-root reference or a different session is not used. For this path correction, update the feature branch and rerun `canary-design` only; no BRAIN re-read or Simulation is required.

#### Round-two offline coverage and candidate shortlist

Run `python -m alpha_research --state-root <same-root> canary-shortlist --plan-id <original-plan-id>` after successful `canary-design`. It verifies the saved design, previously bound field/operator snapshots and source diagnosis, then records a **research order, not a performance ranking**. The original `analyst_revision_ratio_to_close_ebitda_q1` field's catalog coverage is checked against a clearly labeled **heuristic only** 0.50 research screen, not a BRAIN eligibility rule. Related EBITDA-revision MATRIX fields from the same complete catalog may be displayed with their actual catalog coverage and descriptions; name/description similarity does not prove semantic equivalence, and none are silently substituted into expressions. Each row remains `ready_for_simulation=false`. No Gateway, model, real Simulation, correlation or Alpha Submission is invoked. Output is immutable `state/canary/<plan-id>_round2_shortlist.json`.


#### Round-three offline field and expression review

Run `python -m alpha_research --state-root <same-root> canary-field-review --plan-id <original-plan-id>`. This command revalidates (and, if absent, creates) the previous immutable offline shortlist, loads only its pinned exact-account catalog snapshots, and emits `state/canary/<plan-id>_round3_field_review.json`. The report compares catalog coverage and any available `dateCoverage`, `zeroFraction`, `updateFrequency`, `units`; conservatively classifies candidate fields by observable revision measure/direction/forecast-quarter clues; and statically validates `rank(field)` plus `rank(ts_mean(field,10))` for each listed alternative against full REGULAR operator definitions. No field is approved on name similarity, and a static PASS is not a proven alpha effect. The original 26.57% catalog coverage cannot reveal portfolio nonnull rate, time-series sparsity, or why `CONCENTRATED_WEIGHT` failed. Those are recorded as outstanding measurement requirements. Zero real Simulation, model, correlation or Alpha submission calls are made, and this report is not executable Simulation authorization.

#### Offline research decision after field review

Run `python -m alpha_research --state-root <same-root> canary-field-decision --plan-id <original-plan-id>`. It revalidates the immutable Round 3 field review, source catalogs, and original failed BRAIN receipt and writes `state/canary/<plan-id>_round3_decision.json` exactly once. The report distinguishes the catalog coverage fractions from actual instrument-by-date signal availability and portfolio weights, compares all shortlisted fields without silent substitution, groups economic-measure clues, and issues an **internal research materiality screen** (five absolute percentage points; not a WorldQuant/BRAIN rule). Even a larger gain only triggers manual semantic review. Missing pointwise and portfolio-weight distributions are recorded as unmet evidence requirements. No real Simulation, Alpha submission, model invocation, or correlation verification occurs. An existing decision is immutable and validated on rerun.

#### Canary offline distribution evidence audit

Run `python -m alpha_research --state-root <same-root> canary-distribution-audit --plan-id <original-plan-id>`. The default **input** location is `<state-root>/evidence/canary/<plan-id>/`; an optional explicit `--evidence-dir <path>` overrides it. The command first validates the immutable prior field decision; without input files it returns `DISTRIBUTION_EVIDENCE_MISSING`, a machine-readable contract and null metrics, **never fake coverage or positions**. Complete input is `evidence_manifest.json` plus UTF-8 CSVs: `universe.csv` with `date,instrument_id,sector`, `signal.csv` with `date,instrument_id,field_value` (blank for missing), and `weights.csv` with `date,instrument_id,portfolio_weight` (signed weights allowed). Manifest requires pinned plan, Alpha ID, field ID, USA/D1/TOP3000 scope, export origin descriptions, per-file SHA-256 and boolean self-attestations for universe, signal and weight completeness. All inputs are strictly parsed for dates, duplicate IDs, membership, finite numbers, sizes and hashes. With complete evidence it computes non-null coverage **relative to the exported universe**, sector fractions, observed adjacent-export-date changes, and gross-normalized weight concentration (max, top 5, HHI, effective gross holdings) only if weights are declared exhaustive. Self-attestation is never equated to BRAIN lineage: actual Alpha coverage/RA failure mechanism remain unproven until original export provenance is checked. Content-fingerprinted reports under `state/canary/<plan-id>_distribution_<digest>.json` are immutable; a later revised export produces a new report without replacing history. No network, model, live simulation, tags, Alpha submission or correlation calls occur.

Distribution evidence audit also reports per-date/per-sector nonnull ratios and gross-normalized sector weight shares / maximum sector share, only from the manifest-bound local CSVs. It additionally counts consecutive exported-date runs of unchanged, nonnull source-field values (breaking runs on missing observations). Where both signal and portfolio-weight exports are self-attested exhaustive, it reports the fraction of gross portfolio weight aligned with nonnull vs missing original source-field observations. If either export is incomplete, overlap metrics remain null. Export-date run lengths are not calendar/trading-day update intervals, and weight/signal overlap is an observed export association, **not** proof of actual BRAIN signal mechanics or the RA weight-concentration cause.

#### Canary offline distribution evidence packaging (no BRAIN requests)

If the existing audit returns `DISTRIBUTION_EVIDENCE_MISSING`, first obtain **actual dated, instrument-level** exports from an authorized source. Current project BRAIN read methods provide Alpha details, PnL and catalogs but do **not** establish a supported API for reconstructing every instrument's source field values or portfolio weights. Never synthesize these from aggregate Alpha metrics. Place `universe.csv`, `signal.csv` and `weights.csv` under `<state-root>/evidence/canary/<plan-id>/` using the audit's documented headers, then run:

```powershell
python -m alpha_research --state-root "D:\quant\alpha_explore_agent\alpha-research-live" canary-distribution-package `
  --plan-id c619095d74394a0bb3d89d24910963c6 `
  --universe-source "actual provenance of universe CSV" `
  --signal-source "actual provenance of analyst10 field CSV" `
  --weights-source "actual provenance of original Alpha weights CSV"
```

The command checks the existing immutable field decision and validates each file's structure, dates, identifiers, and numerical data. It calculates SHA-256 checksums, creates `evidence_manifest.json` without overwriting any different existing manifest, and then runs the offline distribution audit. It never starts a Gateway, BRAIN/LLM request, simulation, correlation, tag update or Alpha submission. The three completeness flags `--universe-complete`, `--signal-exhaustive`, `--weights-exhaustive` default to **false**. Only pass a flag when the **actual export process** establishes that its claim is true; don't infer completeness from the approximate catalog coverage or local CSV row counts. Partial exports may still yield bounded sample metrics, but portfolio concentration is omitted unless the weights export is attested exhaustive. If raw CSVs are absent, the tool reports `DISTRIBUTION_SOURCE_CSV_MISSING` and does **not** create fake files or a manifest. Raw exports and local manifest are self-attested until independent platform lineage verification; no result grants simulation authorization. Replacing an export requires explicit versioning/separation rather than rewriting a manifest for the same directory.

#### Pinned Alpha native Recordset observation (explicit BRAIN read authorization)

If instrument-level `universe.csv`, `signal.csv`, and `weights.csv` cannot be sourced, **do not manufacture them** from historical Alpha summary metrics. An alternative diagnostic branch can inspect native BRAIN Alpha recordsets (if the account exposes them):

```powershell
python -m alpha_research --state-root "D:\quant\alpha_explore_agent\alpha-research-live" canary-recordsets-observe `
  --plan-id c619095d74394a0bb3d89d24910963c6 `
  --allow-brain-read
```

Set `BRAIN_CREDENTIALS_FILE` in the environment before invoking this command. Without explicit `--allow-brain-read`, without credentials, or in an offline mock workspace, **no platform connection is made**. The observer uses this project's independent `WorldQuantClient` exclusively for the pinned `GET /alphas/<id>`, `GET /alphas/<id>/recordsets`, and, only when listed, four exact Recordsets: `coverage`, `coverage-by-sector`, `turnover`, `average-value-by-sector`. Authentication may require the client's ordinary `POST /authentication`; no Simulation POST, PATCH, submission, correlation or tag mutation is permitted. First it verifies the Alpha ID, `REGULAR` type, exact original expression, and `EQUITY/USA/Delay 1/TOP3000` settings against the immutable research case. Other recordsets are not fetched. Unsupported, incomplete, invalid or unavailable endpoints remain explicitly unresolved; they do not imply zero coverage or turnover.

Captured, size-bounded JSON responses and their exact SHA-256 fingerprints are stored immutably at `state/canary/<plan-id>_recordsets_<digest>.json`. The CLI emits the report's paths, read status and schema hints, **not** the complete raw data in stdout. The first pass deliberately leaves `ALPHA_RECORDSETS_CAPTURED_SCHEMA_REVIEW_REQUIRED`: parsing requires review of the actual returned schema. Native Recordset coverage describes the Alpha portfolio and **cannot be treated as raw analyst10 DataField availability or per-instrument weight distribution**. It supplements but does not satisfy the three-CSV distribution evidence contract. The command never grants a new Simulation or Candidate admission.

#### Offline typed review of captured Recordsets

Once `canary-recordsets-observe` returns a `recordset_evidence_path`, run:

```powershell
python -m alpha_research --state-root "D:\quant\alpha_explore_agent\alpha-research-live" canary-recordsets-review `
  --plan-id c619095d74394a0bb3d89d24910963c6 `
  --evidence-path "<recordset_evidence_path from previous output>"
```

This second command has **no network or credential requirements**. It revalidates the frozen field decision, Alpha identity/scope, recordset fingerprints and immutable evidence location. It recognizes only typed, dated `coverage.instruments` and `turnover.turnover` columns when BRAIN returns a consistent tabular schema. For these series it reports raw-unit minimum/mean/maximum and missing observations, **without guessing whether percent values require scaling** or mistaking instrument counts for coverage fractions. Industry/sector recordsets remain schema-visible and economically unresolved until reviewed. Invalid, incomplete or unsupported schemas return explicit unresolved reasons; no zero metrics or invented values. Every result is immutable and retains `candidate_eligible=false`, `simulation_authorized=false`. This is a diagnostic alternative, not a replacement for raw `signal.csv` / `weights.csv`.
