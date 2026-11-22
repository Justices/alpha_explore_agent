"""P4.25 separately deployable reference backend for an atomic research Grant.

This is a database/service *component*, not an in-process production
authorization bypass. No orchestrator or BRAIN/LLM client imports it.
Operators must deploy the service independently with custodied signing keys,
independent issuer trust, authentication, durable backups and revocation
distribution. The application never imports or invokes this reference.
"""
import base64
import copy
from contextlib import closing
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_global_consumer import (
    _CONTEXT as ACK_CONTEXT,
)
from alpha_research.orchestration.research_only_admission import (
    _CLAIMS, _CONTEXT as GRANT_CONTEXT, _OPERATION as GRANT_OPERATION,
    _cost_envelope, _trusted_public_key, _utc, _HEX32,
)
from alpha_research.state.fingerprint import canonical_json_bytes, fingerprint_json

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS research_grant_consumptions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    grant_id TEXT NOT NULL UNIQUE,
    approval_id TEXT NOT NULL UNIQUE,
    consumption_id TEXT NOT NULL UNIQUE,
    receipt_sha256 TEXT NOT NULL,
    ack_json TEXT
);
CREATE TABLE IF NOT EXISTS research_grant_revocations (
    grant_id TEXT PRIMARY KEY,
    recorded_at TEXT NOT NULL
);
"""


class ReferenceExternalGrantLedger:
    """Transactionally serialized consumption; isolated deployment required."""

    def __init__(self, database_path, *, signer, operator_trust,
                 consumer_id, key_id, ledger_id):
        if (not isinstance(signer, Ed25519PrivateKey)
                or not all(isinstance(s, str) and
                           s.replace("_", "").replace("-", "").isalnum()
                           and 1 <= len(s) <= 80
                           for s in (consumer_id, key_id, ledger_id))):
            raise OrchestrationError("reference global ledger deployment invalid")
        self.path = Path(database_path)
        self.signer = signer
        self.operator_trust = copy.deepcopy(operator_trust)
        self.consumer_id = consumer_id
        self.key_id = key_id
        self.ledger_id = ledger_id

    def initialize_schema(self):
        """Explicit deployment migration; not called by the research app."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript(CREATE_SQL)

    def _connect(self):
        return sqlite3.connect(str(self.path), timeout=10, isolation_level=None)

    def _grant(self, handoff, receipt, now):
        if (not isinstance(receipt, dict)
                or set(receipt) != {"claims", "signature"}
                or not isinstance(receipt["claims"], dict)
                or set(receipt["claims"]) != _CLAIMS
                or not isinstance(handoff, dict)
                or handoff.get("status") != "AWAITING_INDEPENDENT_GLOBAL_CONSUMER"
                or not isinstance(handoff.get("fixed_claims"), dict)):
            raise OrchestrationError("external Grant request schema invalid")
        claims = receipt["claims"]
        if (type(claims["schema_version"]) is not int
                or claims["schema_version"] != 1
                or claims["operation"] != GRANT_OPERATION
                or claims["allow_brain_simulation"] is not False
                or claims["allow_alpha_submission"] is not False
                or type(claims["max_model_calls"]) is not int
                or type(claims["max_reported_tokens"]) is not int):
            raise OrchestrationError("external Grant operation is not read-only")
        clock = now.astimezone(timezone.utc)
        issued, expires = _utc(claims["issued_at"]), _utc(claims["expires_at"])
        if (issued > clock or expires <= clock
                or expires - issued > timedelta(minutes=15)):
            raise OrchestrationError("external Grant expired or future")
        _cost_envelope(claims)
        try:
            signature = base64.b64decode(receipt["signature"], validate=True)
            if len(signature) != 64:
                raise ValueError("invalid signature length")
            _trusted_public_key(self.operator_trust, claims).verify(
                signature, GRANT_CONTEXT + canonical_json_bytes(claims))
        except (InvalidSignature, ValueError, TypeError, OrchestrationError) as exc:
            # Do not turn untrusted bytes into an accepted grant.
            raise OrchestrationError("external Grant issuer signature invalid") from exc
        fixed = {
            "schema_version": 1,
            "operation": "consume_research_grant_once",
            "task_id": claims["task_id"],
            "workflow": claims["workflow"],
            "grant_id": claims["grant_id"],
            "operator_approval_id": claims["operator_approval_id"],
            "research_receipt_sha256": fingerprint_json(receipt),
            "research_scope_sha256": fingerprint_json(claims["scope"]),
            "model_profile_sha256": claims["model_profile_sha256"],
            "price_evidence_sha256": claims["price_evidence_sha256"],
            "max_model_calls": claims["max_model_calls"],
            "max_total_cost_usd_micros": claims["max_total_cost_usd_micros"],
        }
        if (canonical_json_bytes(handoff["fixed_claims"]) != canonical_json_bytes(fixed)
                or handoff.get("fixed_claims_sha256") != fingerprint_json(fixed)):
            raise OrchestrationError("external Grant handoff binding mismatch")
        return fixed, expires

    def consume_once(self, handoff, receipt, *, now=None):
        """Persist ACK before returning it. Parallel or replayed calls conflict.

        Requires a preinitialized schema. No retry on transport uncertainty:
        a caller must independently read back the ledger instead.
        """
        clock = datetime.now(timezone.utc) if now is None else now
        if not isinstance(clock, datetime) or clock.tzinfo is None:
            raise OrchestrationError("external ledger clock invalid")
        fixed, grant_end = self._grant(handoff, receipt, clock)
        at = clock.astimezone(timezone.utc)
        ack_expiry = min(at + timedelta(seconds=90), grant_end)
        consumption_id = secrets.token_hex(16)
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                revoked = db.execute(
                    "SELECT 1 FROM research_grant_revocations WHERE grant_id=?",
                    (fixed["grant_id"],)).fetchone()
                if revoked:
                    raise OrchestrationError("external research Grant revoked")
                row = db.execute(
                    "INSERT INTO research_grant_consumptions"
                    " (grant_id, approval_id, consumption_id, receipt_sha256)"
                    " VALUES (?, ?, ?, ?)",
                    (fixed["grant_id"], fixed["operator_approval_id"],
                     consumption_id, fixed["research_receipt_sha256"]))
                claims = {
                    **fixed,
                    "consumer_id": self.consumer_id,
                    "key_id": self.key_id,
                    "consumption_id": consumption_id,
                    "ledger_id": self.ledger_id,
                    "ledger_sequence": row.lastrowid,
                    "consumed_at": at.isoformat(),
                    "expires_at": ack_expiry.isoformat(),
                    "outcome": "CONSUMED_ONCE",
                }
                ack = {
                    "claims": claims,
                    "signature": base64.b64encode(
                        self.signer.sign(ACK_CONTEXT + canonical_json_bytes(claims))
                    ).decode(),
                }
                db.execute(
                    "UPDATE research_grant_consumptions SET ack_json=?"
                    " WHERE seq=?", (canonical_json_bytes(ack).decode(),
                                      row.lastrowid))
                db.execute("COMMIT")
                return ack
            except (sqlite3.IntegrityError, sqlite3.OperationalError,
                    OrchestrationError) as exc:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise OrchestrationError(
                    "external research Grant already consumed, revoked or ledger unavailable"
                ) from exc

    def read_consumption(self, grant_id):
        """Read existing committed ACK without a second consumption."""
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT ack_json FROM research_grant_consumptions"
                " WHERE grant_id=?", (grant_id,)).fetchone()
            revoked = db.execute(
                "SELECT 1 FROM research_grant_revocations WHERE grant_id=?",
                (grant_id,)).fetchone()
        return {
            "status": "REVOKED" if revoked else ("CONSUMED_ONCE" if row else "NOT_FOUND"),
            "ack": json.loads(row[0]) if row and row[0] else None,
            "dispatch_authorized": False,
        }

    def revoke(self, grant_id, *, now=None):
        """Operator-controlled revocation; a consumed ACK is not erased."""
        clock = datetime.now(timezone.utc) if now is None else now
        if not isinstance(clock, datetime) or clock.tzinfo is None:
            raise OrchestrationError("external ledger clock invalid")
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR IGNORE INTO research_grant_revocations"
                " (grant_id, recorded_at) VALUES (?, ?)",
                (grant_id, clock.astimezone(timezone.utc).isoformat()))
            db.execute("COMMIT")
