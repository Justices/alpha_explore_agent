"""Durable model-attempt budget. No provider calls and no automatic failover.

Limits:
  * max_calls: hard upper bound on reserved model attempts per research ID.
  * max_reported_tokens: blocks the NEXT call once cumulative reported usage
    reaches the limit; it is NOT a hard dollar cap nor a max-output guarantee.

A reservation is persisted BEFORE calling a model. A crashed / ambiguous
in-flight reservation is never retried automatically. All actions are under
a per-research cross-platform filesystem lock.
"""
import hashlib
import json
from pathlib import Path

from alpha_research.state.store import atomic_write_json
from alpha_research.state.writer_lock import SingleWriterLock


class ModelBudgetError(ValueError):
    pass


def _json_bytes(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _sha(value):
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def validate_policy(policy):
    if not isinstance(policy, dict):
        raise ModelBudgetError("model governance policy must be an object")
    if set(policy) != {"max_calls", "max_reported_tokens", "failover"}:
        raise ModelBudgetError(
            "model governance requires max_calls, max_reported_tokens, failover")
    for field in ("max_calls", "max_reported_tokens"):
        limit = policy[field]
        if type(limit) is not int or not 1 <= limit <= 10_000_000:
            raise ModelBudgetError(f"{field} must be a positive bounded integer")
    if policy["failover"] != "manual":
        raise ModelBudgetError("automatic model failover is not authorized")
    return dict(policy)


class ModelCallLedger:
    SCHEMA_VERSION = 1

    def __init__(self, state_root, policy):
        self.state_root = Path(state_root).expanduser().resolve()
        self.policy = validate_policy(policy)
        self.policy_sha256 = _sha(self.policy)

    def _paths(self, research_id):
        if not isinstance(research_id, str) or not research_id.strip():
            raise ModelBudgetError("research_id required for model call budget")
        stem = hashlib.sha256(research_id.encode("utf-8")).hexdigest()
        root = self.state_root / "logs" / "model_budget"
        return root / (stem + ".json"), root / (stem + ".lock")

    @staticmethod
    def _payload(state):
        return {k: v for k, v in state.items() if k != "state_sha256"}

    def _write(self, path, state):
        state["state_sha256"] = _sha(self._payload(state))
        atomic_write_json(path, state)

    def _load(self, path, research_id):
        if not path.exists():
            return {
                "schema_version": self.SCHEMA_VERSION,
                "research_id": research_id,
                "policy": dict(self.policy),
                "policy_sha256": self.policy_sha256,
                "calls": [],
            }
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ModelBudgetError("model budget ledger unreadable") from exc
        if (not isinstance(state, dict)
                or state.get("schema_version") != self.SCHEMA_VERSION
                or state.get("research_id") != research_id
                or state.get("policy") != self.policy
                or state.get("policy_sha256") != self.policy_sha256
                or state.get("state_sha256") != _sha(self._payload(state))
                or not isinstance(state.get("calls"), list)):
            raise ModelBudgetError("model budget ledger is invalid or policy changed")
        seen = set()
        for item in state["calls"]:
            if (not isinstance(item, dict) or not isinstance(item.get("call_id"), str)
                    or item["call_id"] in seen
                    or item.get("status") not in {"IN_FLIGHT", "OK", "ERROR", "UNKNOWN"}
                    or not isinstance(item.get("task"), str)
                    or not isinstance(item.get("role"), (str, type(None)))
                    or not isinstance(item.get("backend"), (str, type(None)))
                    or not isinstance(item.get("backend_sha256"), (str, type(None)))):
                raise ModelBudgetError("model budget attempt ledger is invalid")
            seen.add(item["call_id"])
            for field in ("token_in", "token_out"):
                value = item.get(field)
                if value is not None and (type(value) is not int or value < 0):
                    raise ModelBudgetError("model usage must be nonnegative integer or unknown")
            if item["status"] == "IN_FLIGHT" and (
                    item.get("token_in") is not None
                    or item.get("token_out") is not None):
                raise ModelBudgetError("in-flight model attempt has invalid usage")
        return state

    @staticmethod
    def _summarize(state):
        calls = state["calls"]
        known = sum(
            value for row in calls for value in (row.get("token_in"), row.get("token_out"))
            if type(value) is int
        )
        return {
            "attempts": len(calls),
            "successful": sum(row["status"] == "OK" for row in calls),
            "failed": sum(row["status"] == "ERROR" for row in calls),
            "unknown": sum(row["status"] == "UNKNOWN" for row in calls),
            "unresolved": sum(row["status"] == "IN_FLIGHT" for row in calls),
            "reported_tokens": known,
            "unknown_usage_attempts": sum(
                row["status"] != "IN_FLIGHT" and (
                    row.get("token_in") is None or row.get("token_out") is None
                ) for row in calls
            ),
        }

    def reserve(self, research_id, call_id, *, task, role=None, backend=None,
                backend_sha256=None):
        if not isinstance(call_id, str) or not call_id.strip():
            raise ModelBudgetError("model call_id required")
        if not isinstance(task, str) or not task.strip():
            raise ModelBudgetError("model task required")
        if backend_sha256 is not None and (
                not isinstance(backend_sha256, str) or len(backend_sha256) != 64):
            raise ModelBudgetError("invalid model backend profile digest")
        path, lock = self._paths(research_id)
        with SingleWriterLock(lock):
            state = self._load(path, research_id)
            usage = self._summarize(state)
            if usage["unresolved"]:
                raise ModelBudgetError(
                    "unresolved model call intent; explicit reconciliation required")
            if usage["unknown"]:
                raise ModelBudgetError(
                    "unknown model outcome blocks this research; start a new research ID")
            for prior in state["calls"]:
                if role is not None and prior["role"] == role and (
                        prior.get("backend") != backend
                        or prior.get("backend_sha256") != backend_sha256):
                    raise ModelBudgetError(
                        "model backend changed within research; use a new research ID")
            if usage["attempts"] >= self.policy["max_calls"]:
                raise ModelBudgetError("model call attempt budget exhausted")
            if usage["reported_tokens"] >= self.policy["max_reported_tokens"]:
                raise ModelBudgetError("reported model token budget exhausted")
            if any(c["call_id"] == call_id for c in state["calls"]):
                raise ModelBudgetError("duplicate model call ID")
            state["calls"].append({
                "call_id": call_id, "task": task, "role": role,
                "backend": backend, "backend_sha256": backend_sha256,
                "status": "IN_FLIGHT",
                "token_in": None, "token_out": None,
            })
            self._write(path, state)

    def settle(self, research_id, call_id, *, success, token_in=None, token_out=None):
        if type(success) is not bool:
            raise ModelBudgetError("model call settlement must be explicit")
        for value in (token_in, token_out):
            if value is not None and (type(value) is not int or value < 0):
                raise ModelBudgetError("reported model tokens must be nonnegative integers")
        path, lock = self._paths(research_id)
        with SingleWriterLock(lock):
            state = self._load(path, research_id)
            matching = [c for c in state["calls"] if c["call_id"] == call_id]
            if len(matching) != 1 or matching[0]["status"] != "IN_FLIGHT":
                raise ModelBudgetError("model call intent missing or already settled")
            item = matching[0]
            item.update({
                "status": "OK" if success else "ERROR",
                "token_in": token_in, "token_out": token_out,
            })
            self._write(path, state)
            return self._summarize(state)

    def acknowledge_unknown(self, research_id, call_id, *, reason):
        """Operator-only decision: close an unresolved intent WITHOUT claiming failure.

        This permanently seals the research as blocked: a new research ID is
        needed for further model work. It is not a remote-provider reconciliation
        nor evidence that the call failed.
        """
        if not isinstance(reason, str) or len(reason.strip()) < 8:
            raise ModelBudgetError("explicit reason (at least 8 characters) required")
        path, lock = self._paths(research_id)
        with SingleWriterLock(lock):
            state = self._load(path, research_id)
            rows = [row for row in state["calls"] if row["call_id"] == call_id]
            if len(rows) != 1 or rows[0]["status"] != "IN_FLIGHT":
                raise ModelBudgetError("unresolved model call ID not found")
            rows[0]["status"] = "UNKNOWN"
            rows[0]["operator_reason"] = reason.strip()[:500]
            self._write(path, state)
            return self._summarize(state)

    def status(self, research_id):
        path, lock = self._paths(research_id)
        with SingleWriterLock(lock):
            state = self._load(path, research_id)
            summary = self._summarize(state)
        return {
            "research_id": research_id,
            "policy": dict(self.policy),
            **summary,
            "blocked": bool(
                summary["unresolved"] or summary["unknown"] or
                summary["attempts"] >= self.policy["max_calls"] or
                summary["reported_tokens"] >= self.policy["max_reported_tokens"]
            ),
            "estimated_cost": None,
            "currency": None,
        }
