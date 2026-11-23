"""Reference authority domain application and durable research accounting.

No network listeners or HTTP credentials. A reference implementation for
offline trust-boundary testing, not a production BRAIN/Provider grant service.
"""
import base64
import copy
import json
import secrets
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_global_consumer import _OPERATION
from alpha_research.orchestration.research_grant_service_reference import ReferenceExternalGrantLedger
from alpha_research.orchestration.research_spend_service_reference import ReferenceExternalSpendLedger
from alpha_research.orchestration.research_model_cost_boundary import ceil_cost_usd_micros
from alpha_research.orchestration.research_model_recovery_reference import (
    RECOVERY_SCHEMA, ReferenceExternalModelRecovery,
)
from alpha_research.orchestration.research_canary_control_reference import ReferenceResearchCanaryControl
from alpha_research.state.fingerprint import canonical_json_bytes, fingerprint_json

_GRANT_ID_CHARS = frozenset("0123456789abcdef")


def _grant_id(value):
    if (not isinstance(value, str) or len(value) != 32
            or any(ch not in _GRANT_ID_CHARS for ch in value)):
        raise OrchestrationError("external service grant ID invalid")
    return value


class ResearchAuthorityApplication:
    """Standalone local control plane: grant and spend share one SQLite file."""

    def __init__(self, database_path, *, consumer_signer, ledger_signer,
                 operator_trust, consumer_id, consumer_key_id, ledger_id,
                 ledger_issuer_id, ledger_key_id, global_max_usd_micros,
                 recovery_trust=None):
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
        self.canary = ReferenceResearchCanaryControl(self.path)
        self.signer = ledger_signer
        self.ledger_issuer_id = ledger_issuer_id
        self.ledger_key_id = ledger_key_id
        self.global_max = global_max_usd_micros
        # Recovery trust is provisioned on the authority server, never in an
        # HTTP request. It must not share the consumer or ledger signing key.
        if recovery_trust is not None:
            if (not isinstance(recovery_trust, dict)
                    or not isinstance(recovery_trust.get("issuers"), dict)):
                raise OrchestrationError("remote recovery trust configuration invalid")
            own_keys = {
                consumer_signer.public_key().public_bytes_raw(),
                ledger_signer.public_key().public_bytes_raw(),
            }
            # Operator consent and provider readback must not share custody.
            if not isinstance(operator_trust, dict):
                raise OrchestrationError("research operator trust invalid")
            operators = operator_trust.get("issuers", {})
            if not isinstance(operators, dict):
                raise OrchestrationError("research operator issuer map invalid")
            for operator in operators.values():
                try:
                    operator_pub = base64.b64decode(
                        operator["public_key_b64"], validate=True)
                except (KeyError, TypeError, ValueError) as exc:
                    raise OrchestrationError(
                        "research operator public key invalid") from exc
                if len(operator_pub) != 32:
                    raise OrchestrationError(
                        "research operator public key size invalid")
                own_keys.add(operator_pub)
            for entry in recovery_trust["issuers"].values():
                try:
                    public = base64.b64decode(
                        entry["public_key_b64"], validate=True)
                except (KeyError, TypeError, ValueError) as exc:
                    raise OrchestrationError(
                        "remote recovery trust key invalid") from exc
                if len(public) != 32 or public in own_keys:
                    raise OrchestrationError(
                        "remote recovery signer must be separate from Grant/ledger")
        self.recovery_trust = copy.deepcopy(recovery_trust)
        self.grants = ReferenceExternalGrantLedger(
            self.path, signer=consumer_signer, operator_trust=operator_trust,
            consumer_id=consumer_id, key_id=consumer_key_id,
            ledger_id=ledger_id, require_canary_arm=True)

    def initialize_schema(self):
        """Explicit migration. No network, provider or BRAIN initialization."""
        self.grants.initialize_schema()
        with closing(self.grants._connect()) as db:
            db.executescript(RECOVERY_SCHEMA)
        self.canary.initialize_schema()

    def _recovery(self, grant_id):
        return ReferenceExternalModelRecovery(self.path, grant_id=grant_id)

    def _spend(self, grant_id, claims):
        return ReferenceExternalSpendLedger(
            self.path, grant_id=grant_id,
            max_model_calls=claims["max_model_calls"],
            max_total_cost_usd_micros=claims["max_total_cost_usd_micros"],
            global_max_total_cost_usd_micros=self.global_max,
            require_canary_arm=True)

    def _consumed(self, grant_id, *, allow_revoked=False):
        grant_id = _grant_id(grant_id)
        with closing(self.grants._connect()) as db:
            row = db.execute(
                "SELECT receipt_sha256,receipt_json,ack_json"
                " FROM research_grant_consumptions WHERE grant_id=?",
                (grant_id,)).fetchone()
            revoked = db.execute(
                "SELECT 1 FROM research_grant_revocations WHERE grant_id=?",
                (grant_id,)).fetchone()
        if not row or not row[2] or (revoked and not allow_revoked):
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
        # The local service emergency-stop is a veto, never permission.
        self.canary.assert_armed()
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
        worst_case = ceil_cost_usd_micros(
            input_tokens=n, output_tokens=out,
            input_rate_micros_per_million=claims[
                "input_rate_micros_per_million"],
            output_rate_micros_per_million=claims[
                "output_rate_micros_per_million"])
        result = self._spend(grant_id, claims).reserve_once(
            task_id=data["task_id"], attempt_id=data["attempt_id"],
            worst_case_usd_micros=worst_case,
            expected_grant_ack_sha256=fingerprint_json(ack))
        return {**result, "total_reserved_worst_case_usd_micros": worst_case,
                "global_model_hard_cap_enforced_at_provider": False,
                "permission_to_dispatch": False}

    def quarantine(self, data):
        """Record ambiguous SDK outcome durably; no budget capacity is freed.

        Revocation must not prevent quarantining an already reserved call.
        This endpoint never retries, dispatches or settles the provider call.
        """
        if (not isinstance(data, dict)
                or set(data) != {"grant_id", "task_id", "attempt_id",
                                 "ack_sha256"}):
            raise OrchestrationError("research quarantine payload invalid")
        grant_id = _grant_id(data["grant_id"])
        with closing(self.grants._connect()) as db:
            row = db.execute(
                "SELECT receipt_json,ack_json"
                " FROM research_grant_consumptions WHERE grant_id=?",
                (grant_id,)).fetchone()
        if not row or not row[0] or not row[1]:
            raise OrchestrationError("research quarantine Grant unknown")
        receipt = json.loads(row[0])
        ack = json.loads(row[1])
        claims = receipt["claims"]
        if (data["task_id"] != claims["task_id"]
                or data["ack_sha256"] != fingerprint_json(ack)):
            raise OrchestrationError("research quarantine ACK or Task mismatch")
        return self._spend(grant_id, claims).mark_unknown(
            data["attempt_id"], task_id=data["task_id"])

    def bind_provider_operation(self, data):
        """Persist a returned SDK operation ID even after Grant revocation."""
        if (not isinstance(data, dict)
                or set(data) != {"grant_id", "task_id", "attempt_id",
                                 "ack_sha256", "provider_operation_id"}):
            raise OrchestrationError("model provider binding schema invalid")
        grant_id = _grant_id(data["grant_id"])
        receipt, ack = self._consumed(grant_id, allow_revoked=True)
        if (data["task_id"] != receipt["claims"]["task_id"]
                or data["ack_sha256"] != fingerprint_json(ack)):
            raise OrchestrationError("model provider binding Task/ACK drift")
        return self._recovery(grant_id).bind_operation(
            task_id=data["task_id"], attempt_id=data["attempt_id"],
            provider_operation_id=data["provider_operation_id"])

    def attempt_readback(self, data):
        """Read the authoritative *local* journal after crash or timeout.

        A NOT_FOUND result is never proof that no provider request happened.
        """
        if (not isinstance(data, dict)
                or set(data) != {"grant_id", "task_id", "attempt_id",
                                 "ack_sha256"}):
            raise OrchestrationError("model recovery snapshot payload invalid")
        grant_id = _grant_id(data["grant_id"])
        receipt, ack = self._consumed(grant_id, allow_revoked=True)
        if (data["task_id"] != receipt["claims"]["task_id"]
                or data["ack_sha256"] != fingerprint_json(ack)):
            raise OrchestrationError("model recovery snapshot Task/ACK drift")
        return self._recovery(grant_id).snapshot(
            task_id=data["task_id"], attempt_id=data["attempt_id"])

    def reconcile_remote_observation(self, data):
        """Admin-only signed report ingestion; never settle or retry a call."""
        if (not isinstance(data, dict)
                or set(data) != {"grant_id", "task_id", "attempt_id",
                                 "ack_sha256", "evidence"}):
            raise OrchestrationError("remote reconciliation payload invalid")
        if self.recovery_trust is None:
            raise OrchestrationError(
                "remote provider readback trust independently unconfigured")
        grant_id = _grant_id(data["grant_id"])
        receipt, ack = self._consumed(grant_id, allow_revoked=True)
        if (data["task_id"] != receipt["claims"]["task_id"]
                or data["ack_sha256"] != fingerprint_json(ack)):
            raise OrchestrationError("remote reconciliation Task/ACK drift")
        return self._recovery(grant_id).record_signed_readback(
            task_id=data["task_id"], attempt_id=data["attempt_id"],
            receipt=receipt, ack=ack, evidence=data["evidence"],
            trust=self.recovery_trust)

    def canary_monitor(self, data):
        if not isinstance(data, dict) or data:
            raise OrchestrationError("canary monitor accepts no caller inputs")
        return self.canary.snapshot()

    def canary_stop(self, data):
        if not isinstance(data, dict) or data:
            raise OrchestrationError("canary stop accepts no caller inputs")
        return self.canary.trigger_stop()

    def canary_preflight(self, data):
        """Offline mock barrier only; no live execution enablement."""
        if (not isinstance(data, dict)
                or set(data) != {"grant_id", "task_id", "ack_sha256"}):
            raise OrchestrationError("canary mock preflight payload invalid")
        from alpha_research.orchestration.research_only_admission import _utc
        grant_id = _grant_id(data["grant_id"])
        receipt, ack = self._consumed(grant_id)
        if (data["task_id"] != receipt["claims"]["task_id"]
                or data["ack_sha256"] != fingerprint_json(ack)
                or _utc(ack["claims"]["expires_at"])
                   <= datetime.now(timezone.utc)):
            raise OrchestrationError("canary mock preflight Task/ACK expired")
        state = self.canary.snapshot()
        if state["stop_state"] != "ARMED":
            raise OrchestrationError("canary emergency stop latched")
        return {
            "status": "OFFLINE_MOCK_CANARY_PREFLIGHT_ONLY",
            "task_id": data["task_id"], "grant_id": grant_id,
            "stop_state": "ARMED",
            "offline_mock_permitted": True,
            "real_model_dispatch_authorized": False,
            "brain_read_authorized": False,
            "permission_to_dispatch": False,
            "automatic_retry_allowed": False,
        }

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


