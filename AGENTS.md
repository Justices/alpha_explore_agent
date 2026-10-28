# nastu_quant Project Agent Contract

This file governs research-agent behavior in this repository. Other
repositories' `AGENTS.md` files are not policy sources for this project.
`BRAIN_FLOW_HOME` locates an integration dependency; it does not change this
contract. Do not edit another repository's rules as part of this project.

Files under `design/` are product requirements and historical design inputs.
They inform implementation but do not silently override this file or the
user's current request. Update this contract when an approved project rule
changes.

## Model and platform responsibilities

- This project may run a separately configured model provider, including
  BigModel, for economic reasoning, hypothesis formation, probe planning,
  expression construction, and result attribution. The provider is selected
  through configuration; its API key is read only from the configured
  environment variable and is never written to state or logs.
- The model must base decisions on supplied live or saved evidence. It must
  state uncertainty and failure criteria, and must not invent data, platform
  settings, field semantics, or successful results.
- Use the existing `brain-flow/scripts` entry points and its `brain_session.py`
  owner for supported Dataset/field screening, expression validation,
  simulation, gate evaluation, audit, and recovery. Resolve the repository via
  `BRAIN_FLOW_HOME` and invoke its tools with the absolute `BRAIN_PYTHON`
  interpreter. Do not create a second BRAIN session or submit simulations by
  calling `cnhkmcp`, raw platform HTTP, or `create_simulation` directly.
- `simulate_batch.py` is the execution entry point. Supply its complete
  pre-backtest research evidence, exact effective settings, case-local field
  cache, and output under `brain-flow/runs/<case_key>/`. Recover through the
  existing script workflow; never repost a possibly submitted simulation.
- If an operation has no supported script entry point, identify the capability
  gap and use only a bounded read-only fallback when available. Do not bypass
  execution validation or use a direct platform write.
- Never submit an Alpha or mutate Alpha properties automatically. Simulation
  completion is not Candidate or submission proof.

## Research scope and evidence

- Region is required. Preserve every user-supplied Region, Dataset, objective,
  and other scope value. Never silently substitute scope.
- Choose omitted scope dimensions only from current supported evidence and
  record the choice and rationale. Fix all effective settings before
  backtesting; a script default is not evidence.
- Keep each expression within one Dataset and the platform's validated field
  and operator constraints. Diagnostics answer predeclared material questions;
  their metrics are not Alpha performance evidence.
- Missing, pending, or unreadable gates are unresolved. Candidate requires the
  complete applicable live audit and read-back; model opinion cannot override
  deterministic failures or missing evidence.
- Use the thresholds in this repository's active configuration and approved
  project requirements. Do not import thresholds from another repository or
  historical design text without an explicit project change.

## State, credentials, and portability

- Store runtime state outside the source tree under `NASTU_QUANT_HOME` or the
  platform-specific per-user default. Keep research evidence in a case-scoped
  directory and retain enough inputs and receipts for recovery.
- Read platform credentials through `BRAIN_CREDENTIALS_FILE` as supported by
  `brain_session.py`. Read model credentials through their configured
  environment variable. Never print or persist credential values.
- Support Windows, macOS, and Linux. Use `pathlib`, expand user and environment
  paths, avoid machine-specific absolute paths, and use argument arrays for
  subprocesses. Apply OS-specific locking and file-permission behavior.
- Keep model context, decisions, and recovery evidence tied to the exact case
  and batch. Do not treat a model-service success response as proof of a BRAIN
  operation or a settled research result.
