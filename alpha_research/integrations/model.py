"""Replaceable JSON decision service for the research agent.

The agent depends only on ``complete_json(task, context)``.
"""
import hashlib
import os
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from alpha_research.integrations.model_cli import CLI_COMMANDS, complete_cli
from alpha_research.integrations.model_http import complete_http, post_json
from alpha_research.progress import write_progress
from alpha_research.state.store import atomic_write_json
from alpha_research.state.fingerprint import canonical_json_bytes as _json_bytes
from alpha_research.integrations.model_errors import ModelError
from alpha_research.integrations.model_journal import ModelCallJournal
from alpha_research.integrations.model_replay import replay_batch_call
from alpha_research.integrations.model_prompts import (
    FIELD_EVIDENCE_KEYS, REQUIRED_FIELD_DIMENSIONS, TASK_INSTRUCTIONS,
    KNOWLEDGE_POLICY,
)


class ConfiguredModelClient:
    """Route one model decision to its transport-specific adapter."""

    def __init__(self, config: dict, transport=None, environ=None):
        self.config = config
        self.transport = transport or post_json
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
        if not isinstance(provider, str):
            raise ModelError("model provider must be a nonempty string")
        if provider in CLI_COMMANDS:
            return complete_cli(self, task, context, thread_path=thread_path)
        if provider in {"openai_compatible", "anthropic_compatible", "json_http"}:
            if thread_path is not None:
                raise ModelError("HTTP model API cannot use a CLI research thread")
            return complete_http(self, task, context)
        raise ModelError(f"unsupported model provider: {provider}")


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
