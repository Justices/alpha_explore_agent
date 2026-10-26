"""Replaceable JSON decision service for the research agent.

The agent depends only on ``complete_json(task, context)``. Two wire
adapters are supplied; additional providers can implement that method.
"""
import json
import os
from urllib.request import Request, urlopen


class ModelError(RuntimeError):
    pass


TASK_INSTRUCTIONS = {
    "select_scope": (
        "Region is user-supplied and must never change. Choose supported Delay, Universe "
        "and Neutralization only from the provided live platform combinations. Preserve "
        "every supplied scope value. Return {delay, universe, neutralization, reason}."
    ),
    "select_dataset": (
        "Choose one dataset_id from the live exact-scope catalog. Explain its economic "
        "relevance and field evidence. Do not change Region, Delay, Universe, or objective. "
        "Return {dataset_id, reason}."
    ),
    "plan_probes": (
        "Before hypothesis design, inspect nominal and usable coverage, refresh rate, "
        "units, typical level, distribution, publication timing, and tradeability. "
        "Use only targeted diagnostic probes for material unknowns. Each probe needs "
        "expression, question, economic_relevance, expected, failure_criterion. "
        "Diagnostic Sharpe is never Alpha performance. Return {probes, reason}."
    ),
    "construct_batch": (
        "State the economic mechanism before expressions. Translate observed field constraints "
        "into design choices; never invent missing probe evidence. Return a JSON object with "
        "hypothesis, expected_sign, structure_window_rationale, failure_criterion, "
        "field_report, and variants. Each variant needs expression and ex-ante reason. "
        "Use one Dataset, at most three fields per expression, one to eight variants; "
        "do not splice signals with + or *, use trade_when/add/multiply, or tune to prior results."
    ),
    "attribute_results": (
        "Report failed checks first, then measured metrics. Compare each result with the "
        "pre-registered failure criterion and classify support, refutation, or insufficient "
        "evidence. Platform ERROR is technical, not hypothesis failure. Do not declare Candidate."
    ),
}


def _post_json(url: str, headers: dict, payload: dict) -> dict:
    request = Request(url, data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json", **headers},
                      method="POST")
    with urlopen(request, timeout=120) as response:
        return json.load(response)


class ConfiguredModelClient:
    def __init__(self, config: dict, transport=None, environ=None):
        self.config = config
        self.transport = transport or _post_json
        self.environ = os.environ if environ is None else environ

    def complete_json(self, task: str, context: dict) -> dict:
        provider = self.config.get("provider")
        url = self.config.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ModelError("model service requires an HTTPS url")
        headers = {}
        key_env = self.config.get("api_key_env")
        if key_env:
            key = self.environ.get(key_env)
            if not key:
                raise ModelError(f"model credential environment variable unavailable: {key_env}")
            headers["Authorization"] = f"Bearer {key}"
        elif provider == "openai_compatible":
            raise ModelError("openai_compatible service requires api_key_env")

        if provider == "openai_compatible":
            model = self.config.get("model")
            if not model:
                raise ModelError("model name is required")
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": json.dumps(
                    {"task": task, "context": context,
                     "instruction": TASK_INSTRUCTIONS.get(task, "Return one JSON object only.")},
                    ensure_ascii=False)}],
                "response_format": {"type": "json_object"},
            }
        elif provider == "json_http":
            payload = {"task": task, "context": context,
                       "instruction": TASK_INSTRUCTIONS.get(task, "Return one JSON object only.")}
        else:
            raise ModelError(f"unsupported model provider: {provider}")

        try:
            response = self.transport(url, headers, payload)
            if provider == "openai_compatible":
                result = json.loads(response["choices"][0]["message"]["content"])
            else:
                result = response["result"]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise ModelError("model service returned an invalid JSON decision") from exc
        if not isinstance(result, dict):
            raise ModelError("model decision must be a JSON object")
        return result
