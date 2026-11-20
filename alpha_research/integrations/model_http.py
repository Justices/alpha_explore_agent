"""HTTPS model API adapter; no CLI Agent execution or BRAIN access."""
import json
from urllib.request import Request, urlopen

from alpha_research.integrations.model_errors import ModelError
from alpha_research.integrations.model_json import decode_decision
from alpha_research.integrations.model_prompts import instruction_for


def post_json(url: str, headers: dict, payload: dict, *, timeout_seconds=120) -> dict:
    request = Request(url, data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json", **headers},
                      method="POST")
    with urlopen(request, timeout=timeout_seconds) as response:
        return json.load(response)


def complete_http(client, task, context):
    config = client.config
    provider = config.get("provider")
    url = config.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise ModelError("model service requires an HTTPS url")
    headers = {}
    key_env = config.get("api_key_env")
    if key_env:
        key = client.environ.get(key_env)
        if not key:
            raise ModelError(f"model credential environment variable unavailable: {key_env}")
        if provider == "anthropic_compatible":
            headers["x-api-key"] = key
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {key}"
    elif provider in {"openai_compatible", "anthropic_compatible"}:
        raise ModelError(f"{provider} service requires api_key_env")

    if provider in {"openai_compatible", "anthropic_compatible"}:
        model = config.get("model")
        if not model:
            raise ModelError("model name is required")
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": json.dumps(
                {"task": task, "context": context,
                 "instruction": instruction_for(task)},
                ensure_ascii=False)}],
        }
        if provider == "openai_compatible":
            if config.get("response_format", "json_object") == "json_object":
                payload["response_format"] = {"type": "json_object"}
            if task == "model_probe":
                # Cap the *request's* generated tokens during an authorized
                # connectivity check; the ledger's reported-token budget only
                # blocks later calls and is not a single-call output limit.
                payload["max_tokens"] = 512
        else:
            payload["max_tokens"] = 8192
            if not url.rstrip("/").endswith("/v1/messages"):
                url = url.rstrip("/") + "/v1/messages"
    elif provider == "json_http":
        payload = {"task": task, "context": context,
                   "instruction": instruction_for(task)}
    else:
        raise ModelError(f"unsupported model provider: {provider}")

    try:
        if client.transport is post_json:
            response = client.transport(
                url, headers, payload,
                timeout_seconds=config.get("timeout_seconds", 120))
        else:
            # Injected fake/custom transport keeps the three-argument API.
            response = client.transport(url, headers, payload)
        client.last_usage = (response.get("usage") if isinstance(response, dict) else None)
        if provider == "openai_compatible":
            result = json.loads(response["choices"][0]["message"]["content"])
        elif provider == "anthropic_compatible":
            content = response["content"]
            message = "".join(block["text"] for block in content
                              if block.get("type") == "text")
            client.last_response_text = message
            result = decode_decision(message)
        else:
            result = response["result"]
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise ModelError("model service returned an invalid JSON decision") from exc
    if not isinstance(result, dict):
        raise ModelError("model decision must be a JSON object")
    return result
