# Research Architecture V1.1

This architecture implements the approved [research scope](RESEARCH_SCOPE.md).
The root [agent contract](../AGENTS.md) and approved scope are normative;
older design files are historical references. The architecture is a target
until each workflow's execution, recovery and audit path is verified.

## Package boundaries

`alpha_research` names the system capability shared by Regular, BRAIN-native
Super, and optimization of existing Alphas. The top-level package is independent
of the selected model provider and orchestration implementation.

| Module | Responsibility | Boundary |
| --- | --- | --- |
| `cli` | Parse commands, compose dependencies, and acquire the state writer lock | Delegates research decisions and evidence checks |
| `orchestration` | Route commands, checkpoint tasks, account for budgets, and coordinate recovery | The task engine uses an injected adapter. The current task adapter is offline-only |
| `workflows.regular` | Discover Campaign scope, schedule Dataset subtasks, run and recover Regular batches | Owns Regular scope and batch decisions |
| `workflows.super_alpha` | Build and execute BRAIN-native Super research | Owns Super scope and mechanism |
| `workflows.optimization` | Read the source Alpha and evaluate independent branches | Uses the source family constraints and a read-only baseline |
| `expressions` | Parse and validate Regular and Super expressions | Both Super and Optimization use the shared selection/combo validator |
| `simulation` | Persist experiment intents, match recovery evidence, and cache backtest evidence | Workflow and task recovery retain their own state transitions |
| `qualification` | Apply Candidate, Preferred, and optional PPA policies and maintain the pools | Gate order and qualification are determined by evidence |
| `state` | Resolve state paths, validate shared records, and implement file writes and locks | Storage is independent of model and BRAIN clients |
| `integrations` | Bridge BRAIN operations and configurable model providers | Callers supply the research scope and operation inputs |
| `knowledge`, `config` | Supply research context and effective configuration | Workflows consume the context and persist decision provenance |

The CLI composes the system. Command dispatch selects the workflow, which
owns scope resolution and experiment decisions. Workflows receive platform
operations through an injected gateway and share deterministic validation,
qualification, and state services. Optimization reuses expression validation
without importing the Super workflow. The task engine operates on adapter
outputs and saved records. The driver is a separate process behind the BRAIN
gateway, with a local JSON protocol.


## Decision and evidence flow

Each workflow separates four layers from the confirmed conversation:
Workflow Scope, Research Scope, Research Profile, and Execution Policy.
User constraints and approved system rules are immutable inputs. Model
decisions may fill only open choices. The workflow validates each decision
against current platform evidence and records the choice, rationale, rule
source hashes, and exact effective settings before an experiment.

Platform responses, model decisions and local audit results are separate
artifacts. Simulation intents precede any POST. Recovery reconciles uncertain
intents before further work. Candidate status requires the complete applicable
audit; neither a model response nor simulation completion can promote it.

## Delivery sequence

1. Regular: complete independent Dataset subtask records, bounded scheduling,
   recovery, coverage accounting and PPA Profile.
2. Optimization: capture a read-only source; derive family-specific branches,
   simulate with the appropriate mechanism, compare against baseline and
   audit each branch independently.
3. Super: inspect live BRAIN selection/combo settings and ACTIVE submitted
   Alpha pool; implement native simulation, recovery and Super audit.
4. Verify execution, recovery and qualification through all three routes.
