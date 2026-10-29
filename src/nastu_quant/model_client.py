"""Replaceable JSON decision service for the research agent.

The agent depends only on ``complete_json(task, context)``. Three wire
adapters are supplied; additional providers can implement that method.
"""
import json
import hashlib
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4

from .state_store import atomic_write_json


class ModelError(RuntimeError):
    pass


TASK_INSTRUCTIONS = {
    "select_scope": (
        "Region is user-supplied and must never change. Choose supported Delay, Universe "
        "and Neutralization only from the provided live platform combinations. Choose "
        "unsupplied decay, truncation, and pasteurization from the live settings schema "
        "with an economic rationale. Preserve every supplied scope value. Return "
        "{delay, universe, neutralization, decay, truncation, pasteurization, reason}."
    ),
    "select_dataset": (
        "Choose one dataset_id from the live exact-scope catalog. Explain its economic "
        "relevance and field evidence. Do not change Region, Delay, Universe, or objective. "
        "Return {dataset_id, reason}."
    ),
    "plan_probes": (
        "Before hypothesis design, inspect nominal and usable coverage, refresh rate, "
        "units, typical level, distribution, publication timing, and tradeability. "
        "Choose the necessary core_fields and a required_dimensions map for each field, "
        "including nominal_coverage and usable_observations; justify this in "
        "requirement_reason. Use only targeted probes for material unknowns. Each probe "
        "needs expression, question, economic_relevance, expected, failure_criterion, "
        "and a research object with nonempty idea, data_rationale, operator_rationale, "
        "field_relationship, settings_rationale, expected_behavior, failure_criterion. "
        "Diagnostic Sharpe is never Alpha performance. Return {probes, reason}."
    ),
    "construct_batch": (
        "State the economic mechanism before expressions. Translate observed field constraints "
        "into design choices; never invent missing probe evidence. Return a JSON object with "
        "hypothesis, expected_sign, structure_window_rationale, data_rationale, "
        "operator_rationale, field_relationship, settings_rationale, failure_criterion, "
        "field_report, mechanism_tags, field_generation_tags, and variants. Field reports must cite available evidence for each "
        "required dimension and list nonmaterial unknowns with reason and why_not_material. "
        "Each variant needs expression and ex-ante reason. These top-level rationale "
        "fields plus hypothesis, expected_sign, and failure_criterion must be substantive, "
        "nonempty research evidence suitable for the local pre-backtest receipt. "
        "Use one Dataset, at most three fields per expression, one to eight variants; "
        "do not splice signals with + or *, use trade_when/add/multiply, or tune to prior results. "
        "If draft_proposal is present, review it against the supplied exact operator notes, "
        "preserve the hypothesis and field evidence, and return the complete proposal schema. "
        "If remedy_reference is present, keep its Dataset and mechanism fixed, implement only "
        "its structural action, avoid parameter grids, and cite both diagnosis_id and remedy_id "
        "in every variant reason."
    ),
    "attribute_results": (
        "Report failed checks first, then measured metrics. Compare each result with the "
        "pre-registered failure criterion and classify support, refutation, or insufficient "
        "evidence. Platform ERROR is technical, not hypothesis failure. Do not declare Candidate."
    ),
    "diagnose_failures": (
        "Explain the deterministic signatures without changing the gate verdict. Choose only "
        "among remedies listed for each Alpha. If no verified remedy is listed, recommend "
        "no_known_remedy, abandon, or mechanism_review; never invent an action or remedy_id. "
        "Return {alphas: {alpha_id: {recommendation, remedy_id, rationale, evidence_refs}}}. "
        "recommendation is no_known_remedy, abandon, mechanism_review, or use_remedy. "
        "use_remedy is allowed only when remedy_id exactly matches a verified candidate."
    ),
    "review_candidate": (
        "Review the verified preregistration, live checks, correlations and result for "
        "unresolved robustness or trade-offs. Return {verdict, reason, evidence_refs}; "
        "verdict must be CLEAR, ROBUSTNESS_REVIEW, or TRADEOFF_REVIEW. Do not mark CLEAR "
        "when evidence is missing or a warning is unresolved. Never authorize submission."
    ),
    "decide_next": (
        "Use the attributed result and prior case batches to choose a new independently "
        "justified research direction or pause. Return {action, reason}; action is "
        "continue or pause. Do not repeat expressions, relabel an old branch to reset "
        "its budget, or switch a user-fixed Dataset. A weak batch alone is not economic "
        "exhaustion. Leave unresolved reviews pending and continue only valid work."
    ),
}

KNOWLEDGE_POLICY = (
    "Use only the supplied knowledge_pack. Treat l1_rules with source_kind=normative as "
    "controlling. l1_rules marked advisory_only are secondary and must be ignored on conflict. "
    "L3 entries marked prior_advisory are candidate historical priors, not policy or causal proof. "
    "Do not treat L3 or field snapshots as current platform facts; current facts come from the "
    "live API evidence in this request. Cite pack source and section/record when relying on it. "
    "An absent exact L2 note means its semantics are unknown from local notes; do not invent one."
)


def _post_json(url: str, headers: dict, payload: dict) -> dict:
    request = Request(url, data=json.dumps(payload).encode("utf-8"),
                      headers={"Content-Type": "application/json", **headers},
                      method="POST")
    with urlopen(request, timeout=120) as response:
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
                     "instruction": TASK_INSTRUCTIONS.get(task, "Return one JSON object only.")
                                   + " " + KNOWLEDGE_POLICY},
                    ensure_ascii=False)}],
            }
            if provider == "openai_compatible":
                payload["response_format"] = {"type": "json_object"}
            else:
                payload["max_tokens"] = 8192
                if not url.rstrip("/").endswith("/v1/messages"):
                    url = url.rstrip("/") + "/v1/messages"
        elif provider == "json_http":
            payload = {"task": task, "context": context,
                       "instruction": TASK_INSTRUCTIONS.get(task, "Return one JSON object only.")
                                     + " " + KNOWLEDGE_POLICY}
        else:
            raise ModelError(f"unsupported model provider: {provider}")

        self.last_usage = None
        self.last_response_text = None
        try:
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


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


class DecisionRecorder:
    """Record each model input/output and append a hash-indexed audit line."""

    def __init__(self, model, state_root: Path):
        self.model = model
        self.state_root = Path(state_root).resolve()
        self.logs_root = self.state_root / "logs"
        self.global_calls_root = self.logs_root / "model_calls"
        self.log_path = self.logs_root / "model_log.jsonl"
        self.session_id = None
        self.batch_dir = None
        self.session_calls = []

    def begin_session(self, session_id=None):
        self.session_id = session_id or uuid4().hex
        self.batch_dir = None
        self.session_calls = []
        return self.session_id

    def bind_batch(self, batch_dir: Path):
        self.batch_dir = Path(batch_dir).resolve()
        target = self.batch_dir / "model_calls"
        target.mkdir(parents=True, exist_ok=True)
        manifest_path = self.batch_dir / "model_call_manifest.json"
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            previous = {}
        except (OSError, ValueError) as exc:
            raise ModelError("batch model call manifest is unreadable") from exc
        entries = {row.get("call_id"): row for row in previous.get("calls", [])
                   if isinstance(row, dict) and row.get("call_id")}
        for record, context_path, response_path, record_path in self.session_calls:
            copied_context = target / context_path.name
            copied_response = target / response_path.name
            copied_record = target / record_path.name
            shutil.copy2(context_path, copied_context)
            if response_path.exists():
                shutil.copy2(response_path, copied_response)
            shutil.copy2(record_path, copied_record)
            entries[record["call_id"]] = {
                "call_id": record["call_id"], "task": record["task"],
                "session_id": record.get("session_id"),
                "context_sha256": record["context_sha256"],
                "context_ref": str(copied_context),
                "response_ref": str(copied_response),
                "global_response_ref": str(response_path),
                "record_ref": str(copied_record),
                "global_record_ref": str(record_path)}
        atomic_write_json(manifest_path, {
            "session_ids": sorted({row.get("session_id") for row in entries.values()
                                   if row.get("session_id")}),
            "calls": sorted(entries.values(), key=lambda row: row["call_id"])})
        if self.session_id:
            sessions_root = self.logs_root / "model_sessions"
            sessions_root.mkdir(parents=True, exist_ok=True)
            atomic_write_json(sessions_root / f"{self.session_id}.json", {
                "session_id": self.session_id,
                "batch_ref": str(self.batch_dir),
                "call_ids": [row["call_id"] for row in entries.values()
                             if row.get("session_id") == self.session_id]})

    def _write_log_record(self, record):
        self.logs_root.mkdir(parents=True, exist_ok=True)
        current = []
        if self.log_path.exists():
            current = self.log_path.read_text(encoding="utf-8").splitlines()
        rendered = json.dumps(record, ensure_ascii=False, sort_keys=True)
        replaced = False
        for index, line in enumerate(current):
            try:
                old = json.loads(line)
            except ValueError:
                continue
            if old.get("call_id") == record["call_id"]:
                current[index] = rendered
                replaced = True
                break
        if not replaced:
            current.append(rendered)
        tmp = self.log_path.with_suffix(self.log_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(current) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.log_path)

    def _register_batch_call(self, record, context_path, response_path, record_path):
        if self.batch_dir is None:
            return
        target = self.batch_dir / "model_calls"
        target.mkdir(parents=True, exist_ok=True)
        copied_context = target / context_path.name
        copied_record = target / record_path.name
        shutil.copy2(context_path, copied_context)
        shutil.copy2(record_path, copied_record)
        copied_response = target / response_path.name
        manifest_path = self.batch_dir / "model_call_manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            manifest = {}
        except (OSError, ValueError) as exc:
            raise ModelError("batch model call manifest is unreadable") from exc
        entries = {row.get("call_id"): row for row in manifest.get("calls", [])
                   if isinstance(row, dict) and row.get("call_id")}
        entries[record["call_id"]] = {
            "call_id": record["call_id"], "task": record["task"],
            "session_id": record["session_id"],
            "context_sha256": record["context_sha256"],
            "context_ref": str(copied_context), "response_ref": str(copied_response),
            "global_response_ref": str(response_path),
            "record_ref": str(copied_record), "global_record_ref": str(record_path)}
        atomic_write_json(manifest_path, {
            "session_ids": sorted({row.get("session_id") for row in entries.values()
                                   if row.get("session_id")}),
            "calls": sorted(entries.values(), key=lambda row: row["call_id"])})

    def _replay_batch_call(self, task, context_sha):
        if self.batch_dir is None:
            return None
        manifest_path = self.batch_dir / "model_call_manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        for row in manifest.get("calls", []):
            if row.get("task") != task or row.get("context_sha256") != context_sha:
                continue
            response_path = Path(row.get("response_ref", ""))
            if not response_path.exists() and row.get("global_response_ref"):
                response_path = Path(row["global_response_ref"])
            try:
                payload = json.loads(response_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, KeyError):
                continue
            record_path = Path(row.get("global_record_ref") or "")
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                record = None
            if payload.get("status") in {"ok", "error"} and isinstance(record, dict) and record.get("status") == "in_flight":
                record["status"] = payload["status"]
                record["response_sha256"] = hashlib.sha256(_json_bytes(payload)).hexdigest()
                record["token_in"] = payload.get("token_in")
                record["token_out"] = payload.get("token_out")
                atomic_write_json(record_path, record)
                self._write_log_record(record)
            if payload.get("status") == "ok" and isinstance(payload.get("result"), dict):
                return payload["result"]
        return None

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
        replay = self._replay_batch_call(task, context_sha)
        if replay is not None:
            return replay
        global_context = self.global_calls_root / f"{call_id}.context.json"
        global_response = self.global_calls_root / f"{call_id}.response.json"
        global_record = self.global_calls_root / f"{call_id}.record.json"
        atomic_write_json(global_context, context)
        config = getattr(self.model, "config", {}) or {}
        record = {"ts": datetime.now().astimezone().isoformat(),
                  "call_id": call_id, "session_id": self.session_id,
                  "batch_ref": str(self.batch_dir) if self.batch_dir else
                               f"session:{self.session_id}",
                  "task": task, "context_sha256": context_sha,
                  "context_ref": str(global_context),
                  "knowledge_pack_ref": knowledge_pack_ref,
                  "response_sha256": None, "response_ref": str(global_response),
                  "token_in": None, "token_out": None, "latency_ms": None,
                  "provider": config.get("provider"), "model": config.get("model"),
                  "status": "in_flight"}
        atomic_write_json(global_record, record)
        self._register_batch_call(record, global_context, global_response, global_record)
        self._write_log_record(record)
        started = time.perf_counter()
        response_payload = None
        error = None
        try:
            result = self.model.complete_json(task, context)
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
        atomic_write_json(global_record, record)
        self._write_log_record(record)
        self.session_calls.append((record, global_context, global_response, global_record))
        if self.batch_dir is not None:
            self.bind_batch(self.batch_dir)
        if error is not None:
            raise error
        return return_value
