# Alpha Research Project Agent Contract

This file governs research-agent behavior in this repository. Other
repositories' `AGENTS.md` files are not policy sources for this project.
The brain-flow repository is a read-only reference for development; this agent
must not require, import, or invoke it at runtime. Do not edit another
repository's rules as part of this project.

`design/RESEARCH_SCOPE.md` is the current approved research scope and product
boundary for all three workflows. Apply it together with this contract; for
research-scope conflicts, the approved scope document takes precedence.
Older design files are historical inputs, not active rules. Do not inject
superseded constraints from them. The user's current request takes precedence.

## Model and platform responsibilities

- This project may run a separately configured model provider, including
  BigModel, for economic reasoning, hypothesis formation, probe planning,
  expression construction, and result attribution. The provider is selected
  through configuration; its API key is read only from the configured
  environment variable and is never written to state or logs.
- The model must base decisions on supplied live or saved evidence. It must
  state uncertainty and failure criteria, and must not invent data, platform
  settings, field semantics, or successful results.
- Implement the BRAIN session, Dataset and field screening, expression
  validation, simulation, gate evaluation, audit, and recovery in this project.
  The brain-flow workflow and scripts may be inspected as references, but are
  not runtime dependencies or policy sources.
- Retain complete pre-backtest research evidence, exact effective settings,
  case-local field evidence, and simulation receipts under this project's state
  root. Record non-idempotent simulation intents and recovery identifiers;
  never repost a possibly submitted simulation.
- If platform evidence or an operation is unavailable, report it as unresolved;
  never bypass local validation or substitute a successful result.
- Never submit an Alpha or mutate Alpha properties automatically. Simulation
  completion is not Candidate or submission proof.

## Research scope and evidence

- Region is required. Preserve every user-supplied Region, Dataset, objective,
  and other scope value. Never silently substitute scope.
- For Regular Campaigns, an omitted Dataset permits Agent selection from
  current platform evidence with a recorded rationale. Omitted Category,
  Delay, and Universe require discovery of every platform-valid combination
  consistent with user constraints; budgets limit execution, not requested
  coverage. Report every unexamined or unresolved combination. Fix all
  effective settings before backtesting; a script default is not evidence.
- Keep each Regular expression within one Dataset and the platform's validated
  field and operator constraints. Super Alpha uses BRAIN's dedicated selection
  and combo mechanism. Diagnostics answer predeclared material questions;
  their metrics are not Alpha performance evidence.
- Ordinary Alpha research has no project-imposed PPA operator-count or
  data-field-count limit. Apply PPA rules only when the user explicitly asks
  to explore Power Pool Alpha (PPA): at most 8 operators, at most 3 distinct
  data fields, and Sharpe of at least 1.0. Record the local PPA eligibility
  separately; it does not replace the complete Candidate audit or prove the
  platform has assigned the PPA classification.
- Missing, pending, or unreadable gates are unresolved. Candidate requires the
  complete applicable live audit and read-back; model opinion cannot override
  deterministic failures or missing evidence.
- For Regular research, check the local self-correlation prefilter before
  requesting platform correlations. Compare same-Region non-Power-Pool OS
  Alphas over the most recent four years, using this project's configured
  self-correlation limit. An unavailable comparison remains pending; a
  measured breach blocks the platform correlation request. Resolve case
  pairwise conflicts through the user's decision.
- Use the thresholds in this repository's active configuration and approved
  project requirements. Do not import thresholds from another repository or
  historical design text without an explicit project change.

## State, credentials, and portability

- Store BRAIN Dataset, field, and operator snapshots in the checkout's shared
  `knowledge` directory. Store other runtime state outside the source tree
  under `ALPHA_RESEARCH_HOME`, the legacy `NASTU_QUANT_HOME`, or the platform-specific
  per-user default. Keep case-scoped evidence and receipts for recovery.
- Read platform credentials through `BRAIN_CREDENTIALS_FILE` using this
  project's own session. Read model credentials through their configured
  environment variable. Never print or persist credential values.
- Support Windows, macOS, and Linux. Use `pathlib`, expand user and environment
  paths, avoid machine-specific absolute paths, and use argument arrays for
  subprocesses. Apply OS-specific locking and file-permission behavior.
- Keep model context, decisions, and recovery evidence tied to the exact case
  and batch. Do not treat a model-service success response as proof of a BRAIN
  operation or a settled research result.
