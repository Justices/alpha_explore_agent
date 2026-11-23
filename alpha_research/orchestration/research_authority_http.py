"""Loopback-only, token-isolated HTTP adapter for the reference authority.

The socket listener is completely separate from the durable application.
Even successful local authentication cannot authorize live model or BRAIN IO.
"""
import hmac
import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_authority_application import ResearchAuthorityApplication
from alpha_research.state.fingerprint import canonical_json_bytes

_MAX_BODY = 65_536


def _no_duplicate_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key] = value
    return result


def _forbid_nan(_value):
    raise ValueError("nonfinite JSON number")


def _bounded_content_length(raw):
    """Reject malformed or pathological HTTP lengths before integer parsing."""
    if (not isinstance(raw, str) or len(raw) > 10
            or not raw.isascii() or not raw.isdecimal()):
        return None
    length = int(raw)
    return length if 0 < length <= _MAX_BODY else None


def make_loopback_authority_server(application, *, api_token, admin_token,
                                   host="127.0.0.1", port=0, tls_context=None):
    """Create a loopback-only HTTP server; caller controls its process lifetime.

    These are *transport* credentials. They do not attest independent key
    custody, mTLS, remote identity or provider access.
    """
    if (type(application) is not ResearchAuthorityApplication
            or host != "127.0.0.1"
            or type(port) is not int or not 0 <= port <= 65535
            or not isinstance(api_token, str) or len(api_token) < 32
            or not isinstance(admin_token, str) or len(admin_token) < 32
            or hmac.compare_digest(api_token, admin_token)
            or (tls_context is not None
                and (not isinstance(tls_context, ssl.SSLContext)
                     or tls_context.verify_mode != ssl.CERT_REQUIRED
                     or tls_context.minimum_version < ssl.TLSVersion.TLSv1_2))):
        raise OrchestrationError("loopback authority requires isolated credentials")
    api_bytes = api_token.encode("utf-8")
    admin_bytes = admin_token.encode("utf-8")
    paths = {
        "/v1/grant/consume": application.consume,
        "/v1/grant/readback": application.readback,
        "/v1/spend/reserve": application.reserve,
        "/v1/spend/quarantine": application.quarantine,
        "/v1/spend/operation": application.bind_provider_operation,
        "/v1/spend/attempt": application.attempt_readback,
        "/v1/spend/reconcile": application.reconcile_remote_observation,
        "/v1/canary/monitor": application.canary_monitor,
        "/v1/canary/preflight": application.canary_preflight,
        "/v1/canary/stop": application.canary_stop,
        "/v1/spend/snapshot": application.spend_snapshot,
        "/v1/grant/revoke": application.revoke,
    }

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            """Never log auth header, request body, key or Grant receipts."""

        def _reply(self, status, payload):
            encoded = canonical_json_bytes(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            if self.path != "/healthz":
                self._reply(404, {"error": "not found"})
                return
            self._reply(200, {"status": "LOOPBACK_AUTHORITY_HEALTHY_AUDIT_ONLY",
                              "permission_to_dispatch": False})

        def do_POST(self):
            operation = paths.get(self.path)
            if operation is None:
                self._reply(404, {"error": "not found"})
                return
            token = (admin_bytes if self.path in {
                "/v1/grant/revoke", "/v1/spend/reconcile",
                "/v1/canary/stop"
            } else api_bytes)
            supplied_headers = self.headers.get_all("Authorization", [])
            supplied = supplied_headers[0] if len(supplied_headers) == 1 else ""
            if (len(supplied_headers) != 1
                    or not hmac.compare_digest(supplied.encode("utf-8"),
                                               b"Bearer " + token)):
                self.close_connection = True
                self._reply(401, {"error": "unauthorized"})
                return
            size = _bounded_content_length(self.headers.get("Content-Length"))
            if (self.headers.get("Transfer-Encoding") is not None
                    or size is None
                    or self.headers.get_all("Content-Type", []) != ["application/json"]
                    or len(self.headers.get_all("Content-Length", [])) != 1):
                self.close_connection = True
                self._reply(400, {"error": "invalid request envelope"})
                return
            try:
                raw = self.rfile.read(size)
                request = json.loads(raw.decode("utf-8"),
                                     object_pairs_hook=_no_duplicate_json,
                                     parse_constant=_forbid_nan)
                result = operation(request)
            except (OrchestrationError, ValueError, TypeError, KeyError,
                    UnicodeDecodeError, OverflowError):
                self._reply(409, {"error": "research authority request refused",
                                  "permission_to_dispatch": False})
                return
            except Exception:
                # Provider, SQL and credential details must never leave service.
                self._reply(503, {"error": "research authority unavailable",
                                  "permission_to_dispatch": False})
                return
            self._reply(200, result)

    server = ThreadingHTTPServer((host, port), Handler)
    try:
        if tls_context is not None:
            # Transport still binds loopback only and all API responses deny
            # real dispatch. Peer certificates are verified by SSLContext.
            server.socket = tls_context.wrap_socket(
                server.socket, server_side=True)
        return server
    except Exception:
        server.server_close()
        raise
