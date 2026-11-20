# Agent / LLM registry: start-time role selection

The role is a business contract; an Agent Runtime or LLM API is a registered transport.
The registry is the only backend-definition source; research config defines tasks and roles.

## Files

- model_registry.example.json: editable presets for codex, claude-code, antigravity, deepseek, glm, kimi
- alpha_research/agents/model_registry.json: shipped default registry
- alpha_research/agents/role_profile.json: default research task/role mappings
- config.example.json: research rules plus role profile, with no duplicate backend definitions

## Select and inspect with no external calls

```powershell
python -m alpha_research models list
python -m alpha_research --lead codex --executor glm models inspect
python -m alpha_research --lead kimi --executor claude-code models inspect
python -m alpha_research --registry .\model_registry.example.json --lead deepseek --executor antigravity models inspect
```

Registry selection requires a registered ID for every role. Unknown/missing IDs fail
closed before the workflow calls any Agent runtime, LLM API or BRAIN endpoint.

## Real workflow invocation example (DO NOT run without live BRAIN authorization)

```powershell
python -m alpha_research `
  --config .\config.example.json `
  --registry .\model_registry.example.json `
  --lead codex --executor deepseek `
  run-once --objective "Explore a CHN Alpha hypothesis"
```

The run-once command enters the existing production BRAIN research code path
and may execute a real platform simulation. It is not a safe mock command.

## Registry format

Each entry has kind=agent_runtime or llm_api, an exact provider adapter,
API URL or executable, model identifier, timeout, and api_key_env as applicable.
Credentials themselves belong only in environment variables, never JSON.
Agent runtimes are read-only in the current adapter and are not BRAIN tool owners.
LLM APIs provide reasoning and structured responses; the orchestrator owns tools.

If --registry is omitted, the state-root config/model_registry.json takes
priority; otherwise the bundled registration file is used.

Recovery records lock the selected model profile. Changing the Lead or
Executor while continuing the same study requires new research ID or audited
migration, never implicit replay or repeated simulation.

Tests and `models` commands use no real API keys or BRAIN credentials.
