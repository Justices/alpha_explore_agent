"""Official OpenAI Python SDK adapter for compatible third-party LLMs.

All network I/O is delegated to openai.OpenAI. The provider base_url is an
OpenAI Chat Completions API *root*, not a full /chat/completions URL.
No tools, retries, BRAIN calls or implicit provider fallbacks are authorized.
"""
import json

from alpha_research.integrations.model_errors import ModelError
from alpha_research.integrations.model_json import decode_decision
from alpha_research.integrations.model_prompts import instruction_for
from alpha_research.agents.backend_validation import (
    AgentConfigError, validate_backend,
)


def _usage(response):
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    prompt = getattr(usage, "prompt_tokens", None)
    completion = getattr(usage, "completion_tokens", None)
    result = {}
    if type(prompt) is int and prompt >= 0:
        result["prompt_tokens"] = prompt
    if type(completion) is int and completion >= 0:
        result["completion_tokens"] = completion
    return result or None


def complete_openai_sdk(client, task, context):
    config = client.config
    # Do not trust a runtime caller to have passed through the model
    # registry: the same allowlist/URL/secret-property validation is applied
    # here immediately before any credentials or network client are loaded.
    try:
        validate_backend("runtime", config)
    except AgentConfigError as exc:
        raise ModelError("OpenAI SDK runtime backend contract invalid") from exc
    env_name = config.get("api_key_env")
    if not isinstance(env_name, str) or not env_name:
        raise ModelError("OpenAI SDK backend requires api_key_env")
    secret = client.environ.get(env_name)
    if not secret:
        raise ModelError(f"model credential environment variable unavailable: {env_name}")

    model = config.get("model")
    base_url = config.get("base_url")
    if not isinstance(model, str) or not model.strip():
        raise ModelError("OpenAI SDK backend requires a model identifier")
    if not isinstance(base_url, str) or not base_url.startswith("https://"):
        raise ModelError("OpenAI SDK backend requires an HTTPS base_url")

    kwargs = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": json.dumps(
                {"task": task, "context": context,
                 "instruction": instruction_for(task)},
                ensure_ascii=False, allow_nan=False),
        }],
    }
    response_format = config.get("response_format", "json_object")
    if response_format == "json_object":
        kwargs["response_format"] = {"type": "json_object"}
    elif response_format != "none":
        raise ModelError("unsupported OpenAI SDK response_format")
    if task == "model_probe":
        kwargs["max_tokens"] = 512

    try:
        # Lazy import allows model-free planning and offline diagnostics to run
        # without constructing SDK credentials, connections, or global clients.
        if client.sdk_factory is None:
            from openai import OpenAI
            factory = OpenAI
        else:
            factory = client.sdk_factory
        # OpenAI's default retries (429/5xx/timeouts) are unsafe when the
        # research budget and journal are accounting for ONE provider call.
        # Retrying requires a separate, explicitly authorized reservation.
        with factory(
                api_key=secret, base_url=base_url,
                timeout=config.get("timeout_seconds", 180),
                max_retries=0) as sdk:
            response = sdk.chat.completions.create(**kwargs)
    except ImportError as exc:
        raise ModelError("install the openai Python SDK to use model API backends") from exc
    except Exception as exc:
        # Do not include raw SDK exceptions: providers may echo request details
        # or credentials in diagnostic strings. The recorder retains the
        # terminal error type without exposing a remote response body.
        raise ModelError("OpenAI SDK model request failed") from exc

    # A model alias silently changed by the provider is a different model
    # and potentially a different pricing/risk profile. Reject it rather
    # than declaring the requested backend to have responded successfully.
    response_model = getattr(response, "model", None)
    if response_model is not None and response_model != model:
        raise ModelError("OpenAI SDK response model differs from pinned route")
    client.last_model = response_model or model
    client.last_usage = _usage(response)
    try:
        if (len(response.choices) != 1
                or response.choices[0].finish_reason != "stop"):
            raise ModelError("OpenAI SDK response truncated or incomplete")
        content = response.choices[0].message.content
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        raise ModelError("OpenAI SDK response is missing message content") from exc
    if not isinstance(content, str):
        raise ModelError("OpenAI SDK response content is not text")
    client.last_response_text = content[:10000]
    try:
        decision = decode_decision(content)
    except (TypeError, ValueError) as exc:
        raise ModelError("OpenAI SDK response is not a valid JSON decision") from exc
    if not isinstance(decision, dict):
        raise ModelError("OpenAI SDK decision must be a JSON object")
    return decision
