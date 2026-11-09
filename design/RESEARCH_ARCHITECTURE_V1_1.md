# Research Architecture V1.1

This architecture implements the approved [research scope](RESEARCH_SCOPE.md).
The root [agent contract](../AGENTS.md) and approved scope are normative;
older design files are historical references. The architecture is a target
until each workflow's execution, recovery and audit path is verified.

## Package boundaries

| Package | Owns | Must not own |
| --- | --- | --- |
| `research.regular` | Regular Campaign discovery, Dataset subtasks, combination scheduling, PPA Profile | Super selection/combo rules |
| `research.super` | BRAIN-native Alpha pool, selection/combo design, Super simulation and audit | Regular Dataset/field admission |
| `research.optimization` | Read-only source baseline, Regular/Super branch plans, comparison and branch audit | Mutation of the source Alpha |
| `research.records` | Versioned persisted records and validation | Platform calls or model decisions |
| `gateway` and `platform_runtime` | Authenticated BRAIN operations, receipts and recovery | Economic hypotheses or scope selection |
| `knowledge` | Versioned normative rule injection and advisory evidence | Gate verdict override |
| `agent` | Existing Regular batch engine, pending extraction into the Regular workflow | Cross-workflow routing |

The CLI routes to a workflow. A workflow owns its input validation, scope
resolution, model decisions, state transitions, evidence and output. Shared
platform and knowledge services have narrow interfaces; neither chooses a
research objective. A framework library is optional. Merely wrapping these
packages in LangGraph or another library would not supply missing Super or
Optimization behavior.

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
4. Move legacy Regular batch orchestration out of `agent.py` only after its
   behavior is preserved. Remove superseded active rules, keep historical
   evidence and verify all three workflow routes.
