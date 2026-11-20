# Configurable model profiles by research role

This repository does **not** require Codex for the research lead or Antigravity for the executor.

Roles are stable business contracts. Transport providers (CLI, SDK, HTTP), model
identifiers and credentials are configuration-selected implementations. The
orchestrator still owns all BRAIN simulation authorization, status and recovery.

## Swap model assignments

Use `config.provider-swaps.example.json` as a reference. It is an example,
not a working credential/configuration file. Replace the `*.example.invalid`
HTTPS URLs and `<YOUR_..._MODEL_ID>` placeholders with your actual provider
endpoints and model identifiers, then supply the API key values using the
environment variables specified in `api_key_env`.

The same role can use Codex CLI, Antigravity SDK, a generic OpenAI-compatible
Chat Completions endpoint, an Anthropic-compatible Messages endpoint, or the
existing JSON HTTP adapter. The exact API family must match the provider;
vendor/model labels alone do not guarantee endpoint compatibility.

To change a role's model, edit **only** the role-to-backend binding:

```json
"roles": {
  "research_lead": {"backend": "research_lead_api"},
  "research_executor": {"backend": "research_executor_api"}
}
```

For example, to use Codex for the lead and a configured HTTP model for the
executor, change the lead binding to `alternative_codex` and leave the
executor binding as `research_executor_api`. To use two API models, leave
both API bindings. You can add as many backend definitions as needed, but
unattended role_router rejects unknown provider families and unsafe options.

The model contract is always `complete_json(task, context)`. The task name
selects a logical role, the role selects a backend, and each backend returns
a JSON object. The executor's model receives research contracts and existing
evidence; it does NOT gain permission to run scripts, mutate state, submit
Alphas, or invoke BRAIN. Deterministic tool gateway permissions do not change
when changing models.

## API behavior

- `openai_compatible`: Chat Completions-compatible HTTPS endpoint. Select
  `response_format: "json_object"` for providers that support JSON mode;
  choose `"none"` when unsupported and parse a JSON object response.
- `anthropic_compatible`: Anthropic Messages-compatible HTTPS endpoint,
  requiring an API key environment variable and a model identifier.
- `json_http`: Existing project-specific API contract returning
  `{"result": {...}}`, not a standard universal LLM API.
- Local/offline mock backends are available for testing without API access.
  Provider-specific endpoints or response formats that differ from these
  protocols require a new adapter, not a fake provider name.

Default HTTP transport now honors `timeout_seconds`; tests use fake
transports, not actual network calls.

## Safety and identity

A research run pins the effective model profile hash. A role or model change
during an unfinished run is rejected rather than silently reusing decisions.
Start a **new research ID** for an intentionally different model configuration
or use an explicitly reviewed state migration. Recorded decisions include
the selected role/backend identity and reject incompatible replay. No
automatic failover between model vendors is enabled.

Inspect effective routing without model calls:

```bash
python -m alpha_research.agents --config config.provider-swaps.example.json \
  --task plan_experiments --task prepare_execution --task research_decide
```

These tests verify role selection, HTTP payload normalization, backend
swapping, strict configuration validation, and mock-only orchestration.
They do **not** prove that the configured external vendors accept the
example API endpoints, and they do not perform live simulations.
