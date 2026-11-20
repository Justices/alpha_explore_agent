"""Replaceable JSON decision service for the research agent.

The agent depends only on ``complete_json(task, context)``.
"""
import json
import hashlib
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

from alpha_research.integrations.model_cli import CLI_COMMANDS, CliModelError, LocalCliModelTool
from alpha_research.progress import write_progress
from alpha_research.state.store import atomic_write_json
from alpha_research.state.fingerprint import canonical_json_bytes as _json_bytes
from alpha_research.integrations.model_errors import ModelError
from alpha_research.integrations.model_journal import ModelCallJournal
from alpha_research.integrations.model_replay import replay_batch_call
from alpha_research.integrations.model_prompts import (
    FIELD_EVIDENCE_KEYS, REQUIRED_FIELD_DIMENSIONS, TASK_INSTRUCTIONS,
    KNOWLEDGE_POLICY, instruction_for,
)


def _post_json(url: str, headers: dict, payload: dict, *, timeout_seconds=120) -> dict:
    request = Request(url, data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json", **headers},
                      method="POST")
    with urlopen(request, timeout=timeout_seconds) as response:
        return json.load(response)


def _decision_from_text(message):
    try:
        return json.loads(message)
    except json.JSONDecodeError:
        blocks = re.findall(r"```(?:json)?\s*(.*?)\s*```", message,
                            flags=re.IGNORECASE | re.DOTALL)
        if len(blocks) != 1:
            raise
        return json.loads(blocks[0])


class ConfiguredModelClient:
    def __init__(self, config: dict, transport=None, environ=None):
        self.config = config
        self.transport = transport or _post_json
        self.environ = os.environ if environ is None else environ
        self.last_usage = None
        self.last_response_text = None
        self.last_model = None
        self.last_thread_id = None

    def complete_json(self, task: str, context: dict, *, thread_path=None) -> dict:
        self.last_usage = None
        self.last_response_text = None
        self.last_model = None
        self.last_thread_id = None
        provider = self.config.get("provider")
        if provider in CLI_COMMANDS:
            prompt = json.dumps({
                "task": task, "context": context,
                "instruction": instruction_for(task, forbid_tools=True),
            }, ensure_ascii=False)
            try:
                message, self.last_usage, self.last_model, self.last_thread_id = (
                    LocalCliModelTool(self.config, self.environ).invoke(prompt, thread_path=thread_path))
                if isinstance(message, str):
                    self.last_response_text = message
                result = _decision_from_text(message)
            except CliModelError as exc:
                raise ModelError(str(exc)) from exc
            except (TypeError, ValueError) as exc:
                raise ModelError("local CLI returned an invalid JSON decision") from exc
            if not isinstance(result, dict):
                raise ModelError("model decision must be a JSON object")
            return result
        url = self.config.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ModelError("model service requires an HTTPS url")
        headers = {}
        key_env = self.config.get("api_key_env")
        if key_env:
            key = self.environ.get(key_env)
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
            model = self.config.get("model")
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
                if self.config.get("response_format", "json_object") == "json_object":
                    payload["response_format"] = {"type": "json_object"}
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
            if self.transport is _post_json:
                response = self.transport(
                    url, headers, payload,
                    timeout_seconds=self.config.get("timeout_seconds", 120))
            else:
                # Injected fake/custom transport follows the established
                # three-argument API; never call the actual provider in tests.
                response = self.transport(url, headers, payload)
            self.last_usage = (response.get("usage") if isinstance(response, dict) else None)
            if provider == "openai_compatible":
                result = json.loads(response["choices"][0]["message"]["content"])
            elif provider == "anthropic_compatible":
                content = response["content"]
                message = "".join(block["text"] for block in content
                                  if block.get("type") == "text")
                self.last_response_text = message
                result = _decision_from_text(message)
            else:
                result = response["result"]
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise ModelError("model service returned an invalid JSON decision") from exc
        if not isinstance(result, dict):
            raise ModelError("model decision must be a JSON object")
        return result


def build_model_client(config):
    if not isinstance(config, dict):
        raise ModelError("model configuration must be an object")
    if config.get("provider") != "role_router":
        raise ModelError("model runtime requires provider=role_router and registered roles")
    from alpha_research.agents.router import RoleRoutedModelClient
    return RoleRoutedModelClient(config)


class DecisionRecorder:
    """Record each model input/output and append a hash-indexed audit line."""

    def __init__(self, model, state_root: Path, *, governance=None):
        self.model = model
        self.state_root = Path(state_root).resolve()
        self.logs_root = self.state_root / "logs"
        self.journal = ModelCallJournal(self.state_root)
        self.budget = None
        if governance is not None:
            from alpha_research.agents.governance import ModelCallLedger
            self.budget = ModelCallLedger(self.state_root, governance)
        self.global_calls_root = self.logs_root / "model_calls"
        self.session_id = None
        self.research_id = None
        self.batch_dir = None
        self.session_calls = []

    def begin_session(self, session_id=None, *, research_id=None):
        self.session_id = session_id or uuid4().hex
        self.research_id = research_id or self.session_id
        self.batch_dir = None
        self.session_calls = []
        return self.session_id

    def bind_batch(self, batch_dir: Path):
        self.batch_dir = Path(batch_dir).resolve()
        # Only historical calls need copying when a batch is first bound.
        # The active call is synchronized incrementally at its two status
        # transitions (IN_FLIGHT, then final), not by recopying every call.
        self.journal.sync_batch_calls(
            self.session_calls, batch_dir=self.batch_dir, session_id=self.session_id)

    @staticmethod
    def _usage(model):
        usage = getattr(model, "last_usage", None)
        if not isinstance(usage, dict):
            return None, None
        prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion = usage.get("completion_tokens", usage.get("output_tokens"))
        return (prompt if isinstance(prompt, int) and not isinstance(prompt, bool) else None,
                completion if isinstance(completion, int) and not isinstance(completion, bool) else None)

    def complete_json(self, task: str, context: dict, knowledge_pack_ref=None) -> dict:
        self.logs_root.mkdir(parents=True, exist_ok=True)
        self.global_calls_root.mkdir(parents=True, exist_ok=True)
        if self.session_id is None:
            self.begin_session()
        call_id = uuid4().hex
        context_bytes = _json_bytes(context)
        context_sha = hashlib.sha256(context_bytes).hexdigest()
        config_for = getattr(self.model, "config_for", None)
        config = (config_for(task) if callable(config_for)
                  else getattr(self.model, "config", {})) or {}
        role_for = getattr(self.model, "role_for", None)
        selected_role = role_for(task) if callable(role_for) else None
        backend_name_for = getattr(self.model, "backend_name_for", None)
        selected_backend = backend_name_for(task) if callable(backend_name_for) else None
        route_sha = None
        if selected_role is not None:
            identity = {
                "task": task, "role": selected_role,
                "backend_name": selected_backend, "backend": config,
                "instruction": TASK_INSTRUCTIONS.get(task, "Return one JSON object only."),
                "knowledge_policy": KNOWLEDGE_POLICY,
            }
            route_sha = hashlib.sha256(_json_bytes(identity)).hexdigest()
        backend_digest = hashlib.sha256(_json_bytes(config)).hexdigest()

        def reconcile_verified(recovered_call_id, payload):
            if self.budget is not None:
                self.budget.reconcile_verified_response(
                    self.research_id, recovered_call_id, task=task,
                    role=selected_role, backend=selected_backend,
                    backend_sha256=backend_digest,
                    success=(payload["status"] == "ok"),
                    token_in=payload.get("token_in"),
                    token_out=payload.get("token_out"),
                )

        replay = replay_batch_call(
            self.batch_dir, task, context_sha, route_sha,
            append_event=self.journal.append_event,
            on_verified=reconcile_verified if self.budget is not None else None,
            expected_research_id=self.research_id if self.budget is not None else None)
        if replay is not None:
            return replay
        if self.budget is not None:
            self.budget.reserve(
                self.research_id, call_id, task=task,
                role=selected_role, backend=selected_backend,
                backend_sha256=backend_digest,
            )
        global_context = self.global_calls_root / f"{call_id}.context.json"
        global_response = self.global_calls_root / f"{call_id}.response.json"
        global_record = self.global_calls_root / f"{call_id}.record.json"
        atomic_write_json(global_context, context)
        record = {"ts": datetime.now().astimezone().isoformat(),
                  "call_id": call_id, "session_id": self.session_id,
                  "research_id": self.research_id,
                  "batch_ref": str(self.batch_dir) if self.batch_dir else
                               f"session:{self.session_id}",
                  "task": task, "context_sha256": context_sha,
                  "context_ref": str(global_context),
                  "knowledge_pack_ref": knowledge_pack_ref,
                  "response_sha256": None, "response_ref": str(global_response),
                  "token_in": None, "token_out": None, "latency_ms": None,
                  "provider": config.get("provider"), "model": config.get("model"),
                  "agent_role": selected_role, "agent_backend": selected_backend,
                  "route_sha256": route_sha,
                  "status": "in_flight"}
        atomic_write_json(global_record, record)
        self.journal.sync_batch_calls(
            [(record, global_context, global_response, global_record)],
            batch_dir=self.batch_dir, session_id=self.session_id)
        self.journal.append_event(record)
        started = time.perf_counter()
        response_payload = None
        error = None
        task_label = task if task in TASK_INSTRUCTIONS else "research"
        write_progress(f"正在请求模型决策 · {task_label}")
        try:
            if (config.get("provider") == "codex_cli"
                    and config.get("session", "research") == "research"):
                # Keep legacy per-research Codex sessions unchanged. Routed
                # roles use independent sessions to prevent context leakage.
                namespace = (self.research_id if selected_role is None else
                             f"{self.research_id}:role:{selected_role}")
                thread_key = hashlib.sha256(namespace.encode("utf-8")).hexdigest()
                thread_path = self.state_root / "codex_threads" / thread_key / "thread.json"
                result = self.model.complete_json(task, context, thread_path=thread_path)
            else:
                # Explicit Codex session=ephemeral does not create a thread.
                result = self.model.complete_json(task, context)
            if not isinstance(result, dict):
                raise ModelError("model decision must be a JSON object")
            token_in, token_out = self._usage(self.model)
            response_payload = {"status": "ok", "result": result,
                                "token_in": token_in, "token_out": token_out}
            return_value = result
        except Exception as exc:
            token_in, token_out = self._usage(self.model)
            response_payload = {"status": "error", "error_type": type(exc).__name__,
                                "error": str(exc), "token_in": token_in,
                                "token_out": token_out}
            service_text = getattr(self.model, "last_response_text", None)
            if isinstance(service_text, str):
                response_payload["service_text"] = service_text[:10000]
            error = exc
            return_value = None
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        atomic_write_json(global_response, response_payload)
        response_sha = hashlib.sha256(_json_bytes(response_payload)).hexdigest()
        record.update({"response_sha256": response_sha, "token_in": token_in,
                       "token_out": token_out, "latency_ms": latency_ms,
                       "status": response_payload["status"]})
        actual_model = getattr(self.model, "last_model", None)
        if isinstance(actual_model, str) and actual_model:
            record["model"] = actual_model
        codex_thread_id = getattr(self.model, "last_thread_id", None)
        if isinstance(codex_thread_id, str):
            record["codex_thread_id"] = codex_thread_id
        atomic_write_json(global_record, record)
        self.journal.append_event(record)
        call = (record, global_context, global_response, global_record)
        self.session_calls.append(call)
        self.journal.sync_batch_calls(
            [call], batch_dir=self.batch_dir, session_id=self.session_id)
        if self.budget is not None:
            self.budget.settle(
                self.research_id, call_id, success=(error is None),
                token_in=token_in, token_out=token_out,
            )
        if error is not None:
            write_progress(f"模型调用失败 · {task_label}")
            raise error
        write_progress(f"模型已返回 · {task_label}")
        return return_value
