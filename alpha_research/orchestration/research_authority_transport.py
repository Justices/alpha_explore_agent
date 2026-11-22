"""P5.1 loopback-only independent research authority service (offline pilot).

Run in a separately provisioned process with independently protected keys.
HTTP is *only* allowed on 127.0.0.1, authenticated with per-role secrets.
Never expose it on a public interface or assume bearer auth replaces mTLS.
No endpoint grants BRAIN, SDK, Simulation or Alpha submission permission.
"""
import base64
import hmac
import json
import secrets
from contextlib import closing
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_global_consumer import _OPERATION
from alpha_research.orchestration.research_grant_service_reference import (
    ReferenceExternalGrantLedger,
)
from alpha_research.orchestration.research_spend_service_reference import (
    ReferenceExternalSpendLedger,
)
from alpha_research.state.fingerprint import canonical_json_bytes, fingerprint_json

_MAX_BODY = 65_536
_GRANT_ID_CHARS = frozenset("0123456789abcdef")


def _grant_id(value):
    if (not isinstance(value, str) or len(value) != 32
            or any(ch not in _GRANT_ID_CHARS for ch in value)):
        raise OrchestrationError("external service grant ID invalid")
    return value


def _no_duplicate_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key] = value
    return result


def _forbid_nan(_value):
    raise ValueError("nonfinite JSON number")


class ResearchAuthorityApplication:
    """Standalone local control plane: grant and spend share one SQLite file."""

    def __init__(self, database_path, *, consumer_signer, ledger_signer,
                 operator_trust, consumer_id, consumer_key_id, ledger_id,
                 ledger_issuer_id, ledger_key_id, global_max_usd_micros):
        if (not isinstance(consumer_signer, Ed25519PrivateKey)
                or not isinstance(ledger_signer, Ed25519PrivateKey)
                or consumer_signer.public_key().public_bytes_raw()
                   == ledger_signer.public_key().public_bytes_raw()
                or type(global_max_usd_micros) is not int
                or not 1 <= global_max_usd_micros <= 100_000_000):
            raise OrchestrationError("research authority service provision invalid")
        for value in (ledger_issuer_id, ledger_key_id):
            if (not isinstance(value, str) or len(value) not in range(1, 81)
                    or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
                           for ch in value)):
                raise OrchestrationError("research ledger identity invalid")
        self.path = Path(database_path)
        self.signer = ledger_signer
        self.ledger_issuer_id = ledger_issuer_id
        self.ledger_key_id = ledger_key_id
        self.global_max = global_max_usd_micros
        self.grants = ReferenceExternalGrantLedger(
            self.path, signer=consumer_signer, operator_trust=operator_trust,
            consumer_id=consumer_id, key_id=consumer_key_id,
            ledger_id=ledger_id)

    def initialize_schema(self):
        """Explicit migration. No network, provider or BRAIN initialization."""
        self.grants.initialize_schema()

    def _spend(self, grant_id, claims):
        return ReferenceExternalSpendLedger(
            self.path, grant_id=grant_id,
            max_model_calls=claims["max_model_calls"],
            max_total_cost_usd_micros=claims["max_total_cost_usd_micros"],
            global_max_total_cost_usd_micros=self.global_max)

    def _consumed(self, grant_id):
        grant_id = _grant_id(grant_id)
        with closing(self.grants._connect()) as db:
            row = db.execute(
                "SELECT receipt_sha256,receipt_json,ack_json"
                " FROM research_grant_consumptions WHERE grant_id=?",
                (grant_id,)).fetchone()
            revoked = db.execute(
                "SELECT 1 FROM research_grant_revocations WHERE grant_id=?",
                (grant_id,)).fetchone()
        if not row or not row[2] or revoked:
            raise OrchestrationError("external service Grant absent or revoked")
        receipt = json.loads(row[1])
        ack = json.loads(row[2])
        if (fingerprint_json(receipt) != row[0]
                or ack["claims"]["grant_id"] != grant_id
                or ack["claims"]["research_receipt_sha256"] != row[0]):
            raise OrchestrationError("external service Grant storage mismatch")
        return receipt, ack

    def consume(self, data):
        if not isinstance(data, dict) or set(data) != {"handoff", "receipt"}:
            raise OrchestrationError("research consumption payload invalid")
        handoff, receipt = data["handoff"], data["receipt"]
        if (not isinstance(receipt, dict)
                or not isinstance(receipt.get("claims"), dict)):
            raise OrchestrationError("research signed Grant missing")
        claims = receipt["claims"]
        grant_id = _grant_id(claims.get("grant_id"))
        # Validate issuer signature and fixed handoff *before* budgeting.
        self.grants._grant(
            handoff, receipt, datetime.now(timezone.utc))
        spend = self._spend(grant_id, claims)
        spend.initialize_schema()
        ack = self.grants.consume_once(handoff, receipt)
        return {
            "status": "GRANT_CONSUMED_INDEPENDENT_PROCESS_AUDIT_ONLY",
            "ack": ack, "grant_id": grant_id,
            "external_model_dispatch_authorized": False,
            "brain_read_authorized": False,
            "permission_to_dispatch": False,
        }

    def readback(self, data):
        if not isinstance(data, dict) or set(data) != {"grant_id"}:
            raise OrchestrationError("research ledger readback payload invalid")
        receipt, ack = self._consumed(_grant_id(data["grant_id"]))
        claims = ack["claims"]
        now = datetime.now(timezone.utc)
        signed = {
            "schema_version": 1,
            "operation": "read_research_grant_ledger",
            "evidence_id": secrets.token_hex(16),
            "task_id": claims["task_id"],
            "research_receipt_sha256": fingerprint_json(receipt),
            "issuer_id": self.ledger_issuer_id,
            "key_id": self.ledger_key_id,
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=90)).isoformat(),
            "grant_id": claims["grant_id"],
            "consumption_id": claims["consumption_id"],
            "ack_sha256": fingerprint_json(ack),
            "ledger_id": claims["ledger_id"],
            "ledger_sequence": claims["ledger_sequence"],
            "consumer_id": claims["consumer_id"],
            "remote_grant_state": "CONSUMED_ONCE",
            "readback_source": "REMOTE_LEDGER",
        }
        evidence = {
            "claims": signed,
            "signature": base64.b64encode(self.signer.sign(
                b"alpha_research/research-grant-ledger/v1:"
                + canonical_json_bytes(signed))).decode("ascii"),
        }
        return {"status": "LEDGER_READBACK_SIGNED_AUDIT_ONLY",
                "evidence": evidence,
                "permission_to_dispatch": False}

    def reserve(self, data):
        if (not isinstance(data, dict)
                or set(data) != {"grant_id", "task_id", "attempt_id",
                                 "input_tokens", "max_output_tokens",
                                 "ack_sha256"}):
            raise OrchestrationError("research spend payload invalid")
        grant_id = _grant_id(data["grant_id"])
        receipt, ack = self._consumed(grant_id)
        claims = receipt["claims"]
        n, out = data["input_tokens"], data["max_output_tokens"]
        if (data["task_id"] != claims["task_id"]
                or type(n) is not int or n < 1
                or type(out) is not int or out < 1
                or n > claims["max_input_tokens_per_call"]
                or out > claims["max_output_tokens_per_call"]
                or data["ack_sha256"] != fingerprint_json(ack)):
            raise OrchestrationError("research spend Task, token or ACK drift")
        worst_case = (
            n * claims["input_rate_micros_per_million"]
            + out * claims["output_rate_micros_per_million"]
            + 999_999
        ) // 1_000_000
        result = self._spend(grant_id, claims).reserve_once(
            task_id=data["task_id"], attempt_id=data["attempt_id"],
            worst_case_usd_micros=worst_case,
            expected_grant_ack_sha256=fingerprint_json(ack))
        return {**result, "total_reserved_worst_case_usd_micros": worst_case,
                "global_model_hard_cap_enforced_at_provider": False,
                "permission_to_dispatch": False}

    def spend_snapshot(self, data):
        if not isinstance(data, dict) or set(data) != {"grant_id"}:
            raise OrchestrationError("research spend snapshot payload invalid")
        receipt, _ack = self._consumed(_grant_id(data["grant_id"]))
        return self._spend(data["grant_id"], receipt["claims"]).snapshot()

    def revoke(self, data):
        if not isinstance(data, dict) or set(data) != {"grant_id"}:
            raise OrchestrationError("research Grant revocation payload invalid")
        grant_id = _grant_id(data["grant_id"])
        self.grants.revoke(grant_id)
        return {"status": "REVOKED", "grant_id": grant_id,
                "permission_to_dispatch": False}


def make_loopback_authority_server(application, *, api_token, admin_token,
                                   host="127.0.0.1", port=0):
    """Create a loopback-only HTTP server; caller controls its process lifetime.

    These are *transport* credentials. They do not attest independent key
    custody, mTLS, remote identity or provider access.
    """
    if (type(application) is not ResearchAuthorityApplication
            or host != "127.0.0.1"
            or type(port) is not int or not 0 <= port <= 65535
            or not isinstance(api_token, str) or len(api_token) < 32
            or not isinstance(admin_token, str) or len(admin_token) < 32
            or hmac.compare_digest(api_token, admin_token)):
        raise OrchestrationError("loopback authority requires isolated credentials")
    api_bytes = api_token.encode("utf-8")
    admin_bytes = admin_token.encode("utf-8")
    paths = {
        "/v1/grant/consume": application.consume,
        "/v1/grant/readback": application.readback,
        "/v1/spend/reserve": application.reserve,
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
            token = (admin_bytes if self.path == "/v1/grant/revoke"
                     else api_bytes)
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode("utf-8"),
                                       b"Bearer " + token):
                self.close_connection = True
                self._reply(401, {"error": "unauthorized"})
                return
            size = self.headers.get("Content-Length")
            if (self.headers.get("Transfer-Encoding") is not None
                    or not isinstance(size, str)
                    or not size.isascii() or not size.isdecimal()
                    or not 0 < int(size) <= _MAX_BODY
                    or self.headers.get("Content-Type") != "application/json"):
                self.close_connection = True
                self._reply(400, {"error": "invalid request envelope"})
                return
            try:
                raw = self.rfile.read(int(size))
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

    return ThreadingHTTPServer((host, port), Handler)
