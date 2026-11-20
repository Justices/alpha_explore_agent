# Research role configuration

The single supported launch-time model selection is documented in
[MODEL_AGENT_REGISTRY.md](MODEL_AGENT_REGISTRY.md).

Research tasks use roles such as research_lead and research_executor. The
registry defines Agent runtimes or LLM APIs, and the CLI selects entries by
--lead and --executor without changing the research workflow implementation.

For provider integration internals, see
[CONFIGURABLE_RESEARCH_RUNTIME.md](CONFIGURABLE_RESEARCH_RUNTIME.md).
