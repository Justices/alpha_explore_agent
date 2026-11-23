"""P5.1 strictly loopback-only research authority client.

No auto-retry: lost responses to grant consumption and spend reservations
must be reconciled through readback by an operator, never reposted.
This is NOT wired into the production SDK, driver or orchestration adapter.
"""
import hmac
import http.client
import json

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.state.fingerprint import canonical_json_bytes

_ROUTES = {
    "consume": "/v1/grant/consume",
    "readback": "/v1/grant/readback",
    "reserve": "/v1/spend/reserve",
    "quarantine": "/v1/spend/quarantine",
    "operation": "/v1/spend/operation",
    "attempt": "/v1/spend/attempt",
    "reconcile": "/v1/spend/reconcile",
    "monitor": "/v1/canary/monitor",
    "canary_preflight": "/v1/canary/preflight",
    "stop": "/v1/canary/stop",
    "snapshot": "/v1/spend/snapshot",
    "revoke": "/v1/grant/revoke",
}


class LoopbackAuthorityClient:
    """Pilot-only authenticated client with no fallback or implicit retry."""

    def __init__(self, host, port, *, api_token, admin_token=None,
                 timeout_seconds=3):
        if (host != "127.0.0.1"
                or type(port) is not int or not 1 <= port <= 65535
                or not isinstance(api_token, str) or len(api_token) < 32
                or (admin_token is not None
                    and (not isinstance(admin_token, str)
                         or len(admin_token) < 32
                         or hmac.compare_digest(api_token, admin_token)))
                or type(timeout_seconds) not in (float, int)
                or not 0 < timeout_seconds <= 30):
            raise OrchestrationError("research authority client scope invalid")
        self.host, self.port = host, port
        self.api_token = api_token
        self.admin_token = admin_token
        self.timeout = timeout_seconds

    def _post(self, operation, body):
        if operation not in _ROUTES or not isinstance(body, dict):
            raise OrchestrationError("research authority operation invalid")
        token = (self.admin_token if operation in {"revoke", "reconcile", "stop"}
                 else self.api_token)
        if token is None:
            raise OrchestrationError("research authority admin token unavailable")
        payload = canonical_json_bytes(body)
        if len(payload) > 65_536:
            raise OrchestrationError("research authority request too large")
        connection = http.client.HTTPConnection(
            self.host, self.port, timeout=self.timeout)
        try:
            connection.request(
                "POST", _ROUTES[operation], body=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + token,
                    "Accept": "application/json",
                    "Connection": "close",
                })
            response = connection.getresponse()
            raw = response.read(65_537)
            if len(raw) > 65_536 or response.status != 200:
                raise OrchestrationError(
                    "research authority refused operation; no automatic retry")
            result = json.loads(raw.decode("utf-8"))
            if (not isinstance(result, dict)
                    or result.get("permission_to_dispatch") is not False):
                raise OrchestrationError(
                    "research authority failed negative permission assertion")
            return result
        except (OSError, TimeoutError, UnicodeDecodeError, ValueError) as exc:
            raise OrchestrationError(
                "research authority response uncertain; manual readback required"
            ) from exc
        finally:
            connection.close()

    def consume(self, handoff, receipt):
        return self._post("consume", {"handoff": handoff, "receipt": receipt})

    def readback(self, grant_id):
        return self._post("readback", {"grant_id": grant_id})

    def reserve(self, *, grant_id, task_id, attempt_id, input_tokens,
                max_output_tokens, ack_sha256):
        return self._post("reserve", {
            "grant_id": grant_id, "task_id": task_id,
            "attempt_id": attempt_id, "input_tokens": input_tokens,
            "max_output_tokens": max_output_tokens, "ack_sha256": ack_sha256,
        })

    def quarantine(self, *, grant_id, task_id, attempt_id, ack_sha256):
        return self._post("quarantine", {
            "grant_id": grant_id, "task_id": task_id,
            "attempt_id": attempt_id, "ack_sha256": ack_sha256,
        })

    def bind_provider_operation(self, *, grant_id, task_id, attempt_id,
                                ack_sha256, provider_operation_id):
        return self._post("operation", {
            "grant_id": grant_id, "task_id": task_id,
            "attempt_id": attempt_id, "ack_sha256": ack_sha256,
            "provider_operation_id": provider_operation_id,
        })

    def attempt_readback(self, *, grant_id, task_id, attempt_id, ack_sha256):
        return self._post("attempt", {
            "grant_id": grant_id, "task_id": task_id,
            "attempt_id": attempt_id, "ack_sha256": ack_sha256,
        })

    def reconcile_remote_observation(self, *, grant_id, task_id, attempt_id,
                                     ack_sha256, evidence):
        return self._post("reconcile", {
            "grant_id": grant_id, "task_id": task_id,
            "attempt_id": attempt_id, "ack_sha256": ack_sha256,
            "evidence": evidence,
        })

    def canary_monitor(self):
        return self._post("monitor", {})

    def canary_preflight(self, *, grant_id, task_id, ack_sha256):
        return self._post("canary_preflight", {
            "grant_id": grant_id, "task_id": task_id,
            "ack_sha256": ack_sha256,
        })

    def canary_stop(self):
        return self._post("stop", {})

    def snapshot(self, grant_id):
        return self._post("snapshot", {"grant_id": grant_id})

    def revoke(self, grant_id):
        return self._post("revoke", {"grant_id": grant_id})
