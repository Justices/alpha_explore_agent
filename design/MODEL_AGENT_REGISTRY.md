# Agent / LLM registry: start-time role selection

The role is a business contract; an Agent Runtime or LLM API is a registered transport.
The registry is the only backend-definition source; research config defines tasks and roles.

## Files

- alpha_research/agents/model_registry.json: shipped presets for codex, claude-code, antigravity, deepseek, glm, kimi; override through --registry or the state-root config
- alpha_research/agents/role_profile.json: default research task/role mappings
- config.example.json: research rules only; roles reside solely in the bundled role_profile.json

## Select and inspect with no external calls

```powershell
python -m alpha_research models list
python -m alpha_research --lead codex --executor glm models inspect
python -m alpha_research --lead kimi --executor claude-code models inspect
python -m alpha_research --registry .\alpha_research/agents/model_registry.json --lead deepseek --executor antigravity models inspect
```

Registry selection requires a registered ID for every role. Unknown/missing IDs fail
closed before the workflow calls any Agent runtime, LLM API or BRAIN endpoint.

## Real workflow invocation example (DO NOT run without live BRAIN authorization)

```powershell
python -m alpha_research `
  --config .\config.example.json `
  --registry .\alpha_research/agents/model_registry.json `
  --lead codex --executor deepseek `
  run-once --objective "Explore a CHN Alpha hypothesis"
```

The run-once command enters the existing production BRAIN research code path
and may execute a real platform simulation. It is not a safe mock command.

## Registry format

Each entry has kind=agent_runtime or llm_api, an exact provider adapter,
API base_url or executable, model identifier, timeout, and api_key_env as applicable.
The llm_api provider is exclusively openai_sdk, powered by the official
Python `openai` package. Requests use `OpenAI(base_url=..., api_key=...,
max_retries=0).chat.completions.create()` and never invoke a raw urllib model
transport. A base_url is the API *root*, not /chat/completions.

The bundled GLM registration uses the mainland BigModel endpoint
`https://open.bigmodel.cn/api/paas/v4` with the `ZAI_API_KEY` environment
variable. A credential from the Z.ai international service instead requires
an explicitly configured `https://api.z.ai/api/paas/v4` registration.
Do not mix credentials across the two services or silently replace an
existing research model configuration.
Credentials themselves belong only in environment variables, never JSON.
Agent runtimes are read-only in the current adapter and are not BRAIN tool owners.
LLM APIs provide reasoning and structured responses; the orchestrator owns tools.

If --registry is omitted, the state-root config/model_registry.json takes
priority; otherwise the bundled registration file is used.

Recovery records lock the selected model profile. Changing the Lead or
Executor while continuing the same study requires new research ID or audited
migration, never implicit replay or repeated simulation.

Offline tests and `models list`/`inspect`/`doctor` do not call providers or BRAIN.
`models smoke --allow-model-call` is an **explicit real model API call** and
uses the configured environment credentials; it does not invoke BRAIN.
See [Current Design](CURRENT_DESIGN.md) for the tested versus unverified boundaries.
