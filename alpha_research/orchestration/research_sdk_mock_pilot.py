"""P5.3 real OpenAI Python SDK protocol test with a forced in-process mock.

This deliberately executes the actual installed openai.ChatCompletions SDK path,
but sdk_http.MockTransport is constructed *inside* this function, no credentials
are read and no network connection is possible. It does NOT authorize paid
model dispatch. Future real dispatch needs a separately reviewed, externally
trusted production adapter; a fake signed report cannot enable it.

Reservation precedes SDK invocation. Outcomes that cannot be reconciled from
the mock response are quarantined durably; no automatic SDK or attempt retry.
"""
import json
import re

from openai import OpenAI

# OpenAI Python SDK 3.x uses HTTPX2; SDK 2.x uses legacy HTTPX.
# Bind the native transport family instead of making an unsafe custom-client
# compatibility assumption. Neither family can contact a network here.
try:
    from openai import DefaultHttpx2Client as _NativeSdkClient  # noqa: F401
except ImportError:
    import httpx as sdk_http
else:
    import httpx2 as sdk_http

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_grant_ledger import (
    inspect_research_grant_ledger,
)
from alpha_research.orchestration.research_price_provenance import (
    inspect_research_price_provenance,
)
from alpha_research.orchestration.research_sdk_boundary import (
    build_research_sdk_request_plan, inspect_research_sdk_usage,
)
from alpha_research.orchestration.research_model_cost_boundary import (
    ceil_cost_usd_micros,
)
from alpha_research.state.fingerprint import fingerprint_json

_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_ROLES = frozenset({"system", "user"})
_MAX_PROMPT_CHARS = 32_000


def _validate_messages(messages):
    if (not isinstance(messages, list) or not 1 <= len(messages) <= 8):
        raise OrchestrationError("mock SDK requires a bounded chat transcript")
    safe = []
    for message in messages:
        if (not isinstance(message, dict)
                or set(message) != {"role", "content"}
                or type(message["role"]) is not str
                or message["role"] not in _ROLES
                or type(message["content"]) is not str
                or not message["content"]
                or len(message["content"]) > _MAX_PROMPT_CHARS):
            raise OrchestrationError("mock SDK chat message schema invalid")
        safe.append(dict(message))
    if sum(len(m["content"]) for m in safe) > _MAX_PROMPT_CHARS:
        raise OrchestrationError("mock SDK total prompt length exceeded")
    return safe


def _validate_fixture(fixture):
    if (not isinstance(fixture, dict)
            or set(fixture) != {"content", "prompt_tokens",
                                "completion_tokens", "finish_reason"}
            or type(fixture["content"]) is not str
            or len(fixture["content"]) > 32_000
            or any(type(fixture[k]) is not int or fixture[k] < 0
                   for k in ("prompt_tokens", "completion_tokens"))
            or fixture["finish_reason"] not in ("stop", "length", "content_filter")):
        raise OrchestrationError("mock SDK fixture schema invalid")


def _sdk_response_status(plan, completion):
    """Inspect real SDK ChatCompletion objects; reported use is not billing."""
    try:
        if (completion.model != plan["fixed_sdk_options"]["model"]
                or not isinstance(completion.id, str)
                or not completion.id
                or len(completion.choices) != 1
                or completion.choices[0].index != 0
                or completion.choices[0].finish_reason != "stop"
                or completion.choices[0].message.role != "assistant"
                or type(completion.choices[0].message.content) is not str
                or not completion.choices[0].message.content
                or len(completion.choices[0].message.content) > 32_000
                or completion.usage is None):
            raise OrchestrationError("mock SDK response missing or incomplete")
        usage = {
            "prompt_tokens": completion.usage.prompt_tokens,
            "completion_tokens": completion.usage.completion_tokens,
            "total_tokens": completion.usage.total_tokens,
        }
        report = inspect_research_sdk_usage(plan, usage)
        if report["status"] != "PROVIDER_REPORTED_USAGE_WITHIN_DECLARED_CAPS_AUDIT_ONLY":
            raise OrchestrationError("mock SDK usage exceeded pinned ceiling")
        return usage, completion.choices[0].message.content, completion.id
    except (AttributeError, IndexError, TypeError, ValueError) as exc:
        raise OrchestrationError("mock SDK response invalid") from exc


def run_research_sdk_offline_pilot(
        orchestrator, task_id, model_config, governance_policy,
        receipt, operator_trust, ack, consumer_trust,
        ledger_evidence, ledger_trust, price_evidence, price_trust,
        authority_client, *, attempt_id, task_name, declared_input_tokens,
        max_output_tokens, messages, fixture, fault=None, now=None):
    """Run a single *in-process SDK mock* after the remote authority reserve.

    The external authority and Grant are genuine to this local pilot, but the
    model reply and metering are synthetic. All reservations remain consumed.
    """
    if (not isinstance(attempt_id, str) or not _HEX32.fullmatch(attempt_id)
            or fault not in (None, "timeout")
            or not all(callable(getattr(authority_client, name, None))
                       for name in ("reserve", "quarantine",
                                    "bind_provider_operation",
                                    "canary_preflight"))):
        raise OrchestrationError("research SDK pilot attempt/authority invalid")
    safe_messages = _validate_messages(messages)
    if fault is None:
        _validate_fixture(fixture)
    ledger = inspect_research_grant_ledger(
        orchestrator, task_id, model_config, governance_policy,
        receipt, operator_trust, ack, consumer_trust,
        ledger_evidence, ledger_trust, now=now)
    price = inspect_research_price_provenance(
        orchestrator, task_id, model_config, governance_policy,
        receipt, operator_trust, price_evidence, price_trust,
        task_name=task_name, input_tokens=declared_input_tokens,
        max_output_tokens=max_output_tokens, now=now)
    plan = build_research_sdk_request_plan(
        orchestrator, task_id, model_config, governance_policy,
        receipt, operator_trust, task_name=task_name,
        declared_input_tokens=declared_input_tokens,
        max_output_tokens=max_output_tokens, now=now)
    if (price["sdk_request_sha256"]
            != fingerprint_json(plan["fixed_sdk_options"])
            or price["tariff_sha256"] != receipt["claims"]["price_evidence_sha256"]
            or ledger["grant_id"] != ack["claims"]["grant_id"]
            or ledger["ack_sha256"] != fingerprint_json(ack)
            or price["research_receipt_sha256"] != fingerprint_json(receipt)):
        raise OrchestrationError("SDK pilot independent evidence binding mismatch")

    # A dedicated client invokes the independently running authority.
    reservation = authority_client.reserve(
        grant_id=ledger["grant_id"], task_id=task_id,
        attempt_id=attempt_id, input_tokens=declared_input_tokens,
        max_output_tokens=max_output_tokens, ack_sha256=fingerprint_json(ack))
    if (reservation.get("status") != "SPEND_RESERVED_REFERENCE_ONLY"
            or reservation.get("permission_to_dispatch") is not False
            or reservation.get("attempt_id") != attempt_id
            or reservation.get("task_id") != task_id
            or reservation.get("grant_id") != ledger["grant_id"]
            or type(reservation.get("reserved_usd_micros")) is not int
            or reservation["reserved_usd_micros"]
               != plan["worst_case_request_usd_micros"]):
        # Reservation might already be committed even if response is bad.
        # Quarantine via exact same attempt, never issue a second reserve.
        _quarantine(authority_client, ledger["grant_id"], task_id,
                    attempt_id, ack)
        raise OrchestrationError("SDK mock budget receipt mismatch; quarantined")

    # Stop can latch between budget reservation and the SDK mock: recheck
    # with the independent service. A failed check *quarantines* the
    # committed budget, never dispatches or retries. This is not sufficient
    # atomic authorization for a real remote provider and stays mock-only.
    try:
        preflight = authority_client.canary_preflight(
            grant_id=ledger["grant_id"], task_id=task_id,
            ack_sha256=fingerprint_json(ack))
        if (preflight.get("status") != "OFFLINE_MOCK_CANARY_PREFLIGHT_ONLY"
                or preflight.get("offline_mock_permitted") is not True
                or preflight.get("permission_to_dispatch") is not False
                or preflight.get("stop_state") != "ARMED"):
            raise OrchestrationError("SDK mock canary stop preflight invalid")
    except Exception as exc:
        _quarantine(authority_client, ledger["grant_id"], task_id,
                    attempt_id, ack)
        raise OrchestrationError(
            "SDK mock canary preflight failed; reserved spend quarantined"
        ) from exc

    observed = []
    opts = plan["fixed_sdk_options"]
    def handle(request):
        observed.append(request)
        if (request.method != "POST"
                or request.url.host != "sdk-mock.invalid"
                or request.url.path != "/v1/chat/completions"):
            raise OrchestrationError("unexpected mock SDK transport destination")
        payload = json.loads(request.content)
        if (set(payload) != {"model", "messages", "max_tokens", "stream", "n"}
                or payload["messages"] != safe_messages
                or any(type(payload.get(k)) is not type(v) or payload[k] != v
                       for k, v in opts.items())):
            raise OrchestrationError("SDK request shape or hard cap drift")
        if fault == "timeout":
            raise sdk_http.ReadTimeout("synthetic SDK timeout")
        return sdk_http.Response(
            200,
            json={
                "id": "chatcmpl-offline-" + attempt_id,
                "object": "chat.completion",
                "created": 1700000000,
                "model": opts["model"],
                "choices": [{
                    "index": 0, "finish_reason": fixture["finish_reason"],
                    "message": {"role": "assistant",
                                "content": fixture["content"]},
                }],
                "usage": {
                    "prompt_tokens": fixture["prompt_tokens"],
                    "completion_tokens": fixture["completion_tokens"],
                    "total_tokens": (fixture["prompt_tokens"]
                                     + fixture["completion_tokens"]),
                },
            },
        )

    # The only SDK client constructed here is forced to in-process MockTransport:
    # no API key access, no remote DNS, and max_retries=0 regardless of defaults.
    try:
        with sdk_http.Client(transport=sdk_http.MockTransport(handle)) as http_client:
            with OpenAI(
                    api_key="synthetic-offline-token",
                    base_url="http://sdk-mock.invalid/v1",
                    http_client=http_client,
                    max_retries=0,
                    timeout=5.0) as sdk:
                response = sdk.chat.completions.create(
                    model=opts["model"], messages=safe_messages,
                    max_tokens=opts["max_tokens"],
                    stream=opts["stream"], n=opts["n"])
        usage, content, provider_operation_id = _sdk_response_status(plan, response)
        if len(observed) != 1:
            raise OrchestrationError("SDK mock unexpected retry")
        claims = receipt["claims"]
        provider_reported_usd_micros = ceil_cost_usd_micros(
            input_tokens=usage["prompt_tokens"],
            output_tokens=usage["completion_tokens"],
            input_rate_micros_per_million=claims[
                "input_rate_micros_per_million"],
            output_rate_micros_per_million=claims[
                "output_rate_micros_per_million"])
        if provider_reported_usd_micros > reservation["reserved_usd_micros"]:
            raise OrchestrationError("SDK reported charge exceeds reservation")
        binding = authority_client.bind_provider_operation(
            grant_id=ledger["grant_id"], task_id=task_id,
            attempt_id=attempt_id, ack_sha256=fingerprint_json(ack),
            provider_operation_id=provider_operation_id)
        if (binding.get("status") != "PROVIDER_OPERATION_ID_RECORDED_AUDIT_ONLY"
                or binding.get("provider_operation_id") != provider_operation_id
                or binding.get("permission_to_dispatch") is not False):
            raise OrchestrationError(
                "SDK mock operation binding uncertain; reconcile before retry")
    except Exception as exc:
        # Error, HTTP exception, malformed usage or output-length stop:
        # the remote call would have an unknown outcome. Quarantine even
        # when the fault was synthetic, with no fallback or automatic retry.
        _quarantine(authority_client, ledger["grant_id"], task_id,
                    attempt_id, ack)
        raise OrchestrationError(
            "SDK mock outcome uncertain; reserved spend quarantined; no retry"
        ) from exc

    return {
        "schema_version": 1, "task_id": task_id, "attempt_id": attempt_id,
        "status": "SDK_MOCK_COMPLETED_COST_HELD_AUDIT_ONLY",
        "sdk_package_exercised": True,
        "sdk_http_requests": len(observed),
        "sdk_transport": "IN_PROCESS_MOCK_ONLY",
        "sdk_max_retries": 0,
        "sdk_timeout_seconds": 5,
        "model": opts["model"],
        "provider_operation_id": provider_operation_id,
        "provider_operation_id_independently_verified": False,
        "response_content_sha256": fingerprint_json(content),
        "reported_input_tokens": usage["prompt_tokens"],
        "reported_output_tokens": usage["completion_tokens"],
        "provider_reported_usd_micros": provider_reported_usd_micros,
        "reserved_usd_micros": reservation["reserved_usd_micros"],
        "independent_provider_billing_verified": False,
        "provider_invoked": False, "brain_invoked": False,
        "model_call_authorized": False, "brain_read_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "automatic_retry_allowed": False, "permission_to_dispatch": False,
    }


def _quarantine(client, grant_id, task_id, attempt_id, ack):
    try:
        state = client.quarantine(
            grant_id=grant_id, task_id=task_id,
            attempt_id=attempt_id, ack_sha256=fingerprint_json(ack))
        if (state.get("status") != "UNKNOWN_REQUIRES_MANUAL_RECONCILIATION"
                or state.get("permission_to_dispatch") is not False):
            raise OrchestrationError("SDK mock quarantine result invalid")
    except Exception as exc:
        raise OrchestrationError(
            "SDK mock attempt may have started; quarantine uncertain;"
            " operator reconciliation required; never retry"
        ) from exc
