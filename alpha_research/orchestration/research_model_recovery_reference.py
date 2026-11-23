"""P5.4 durable, non-executable provider operation and recovery observation ledger.

All incoming provider operation IDs are **untrusted** until a separately
signed readback matches the specific receipt, ACK, Task and attempt. Even a
valid signature is an audit assertion, NOT evidence of provider billing custody.
Neither settlement nor refund nor SDK retry exists in this reference module.
"""
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_remote_reconciliation import (
    inspect_research_remote_reconciliation,
)
from alpha_research.state.fingerprint import canonical_json_bytes, fingerprint_json

_HEX32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_PROVIDER_ID = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")

RECOVERY_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_model_operations (
    attempt_id TEXT PRIMARY KEY,
    provider_operation_id TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL CHECK(source IN ('SDK_REPORTED','SIGNED_READBACK')),
    recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_model_recovery_observations (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL UNIQUE,
    evidence_sha256 TEXT NOT NULL UNIQUE,
    evidence_json TEXT NOT NULL,
    verdict TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS research_model_recovery_attempt
    ON research_model_recovery_observations(attempt_id);
"""


def _hex32(value):
    return isinstance(value, str) and bool(_HEX32.fullmatch(value))


class ReferenceExternalModelRecovery:
    """Operate on the SAME independently controlled SQLite as the spend ledger."""

    def __init__(self, database_path, *, grant_id):
        if not _hex32(grant_id):
            raise OrchestrationError("model recovery Grant ID invalid")
        self.path = Path(database_path)
        self.grant_id = grant_id

    def _connect(self):
        return sqlite3.connect(str(self.path), timeout=10, isolation_level=None)

    def initialize_schema(self):
        # Explicit deployment migration. Never performed by the research Agent.
        with closing(self._connect()) as db:
            db.executescript(RECOVERY_SCHEMA)

    def _attempt(self, db, task_id, attempt_id):
        if not _hex32(task_id) or not _hex32(attempt_id):
            raise OrchestrationError("model recovery Task/attempt ID invalid")
        row = db.execute(
            "SELECT reserved_usd_micros,status FROM research_model_spend"
            " WHERE grant_id=? AND task_id=? AND attempt_id=?",
            (self.grant_id, task_id, attempt_id)).fetchone()
        if row is None:
            raise OrchestrationError("model recovery attempt not reserved")
        return row

    def bind_operation(self, *, task_id, attempt_id, provider_operation_id,
                       source="SDK_REPORTED"):
        """Persist a provider ID after SDK receipt; no execution or transition.

        Reject replay even when the same ID is supplied again. If this write
        is ambiguous, only readback may recover it; never re-run the model.
        """
        if (not isinstance(provider_operation_id, str)
                or not _PROVIDER_ID.fullmatch(provider_operation_id)
                or source not in {"SDK_REPORTED", "SIGNED_READBACK"}):
            raise OrchestrationError("model recovery operation ID invalid")
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                self._attempt(db, task_id, attempt_id)
                db.execute(
                    "INSERT INTO research_model_operations"
                    " (attempt_id,provider_operation_id,source,recorded_at)"
                    " VALUES (?,?,?,?)",
                    (attempt_id, provider_operation_id, source,
                     datetime.now(timezone.utc).isoformat()))
                db.execute("COMMIT")
            except (sqlite3.Error, OrchestrationError) as exc:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise OrchestrationError(
                    "model operation binding replay, conflict or unavailable"
                ) from exc
        return {
            "status": "PROVIDER_OPERATION_ID_RECORDED_AUDIT_ONLY",
            "task_id": task_id, "attempt_id": attempt_id,
            "provider_operation_id": provider_operation_id,
            "provider_id_independently_verified": False,
            "automatic_retry_allowed": False, "permission_to_dispatch": False,
        }

    def snapshot(self, *, task_id, attempt_id):
        with closing(self._connect()) as db:
            reserved, state = self._attempt(db, task_id, attempt_id)
            op = db.execute(
                "SELECT provider_operation_id,source"
                " FROM research_model_operations WHERE attempt_id=?",
                (attempt_id,)).fetchone()
            observations = db.execute(
                "SELECT COUNT(*),MAX(seq) FROM"
                " research_model_recovery_observations WHERE attempt_id=?",
                (attempt_id,)).fetchone()
            last = (db.execute(
                "SELECT evidence_sha256,verdict FROM"
                " research_model_recovery_observations WHERE seq=?",
                (observations[1],)).fetchone()
                if observations[1] is not None else None)
        return {
            "schema_version": 1, "task_id": task_id, "attempt_id": attempt_id,
            "grant_id": self.grant_id, "spend_state": state,
            "reserved_usd_micros": reserved,
            "provider_operation_id": op[0] if op else None,
            "provider_operation_source": op[1] if op else None,
            "signed_observation_count": observations[0],
            "latest_reconciliation_evidence_sha256": last[0] if last else None,
            "latest_reconciliation_verdict": last[1] if last else None,
            "billing_independently_settled": False,
            "reservation_released": False,
            "safe_to_retry": False, "automatic_retry_allowed": False,
            "permission_to_dispatch": False,
        }

    def record_signed_readback(self, *, task_id, attempt_id, receipt, ack,
                               evidence, trust, now=None):
        """Verify remote readback claims; append one immutable signed observation.

        Any anomaly stays quarantined for manual review. A signature by a
        configured source is NOT an independently verified provider invoice.
        """
        clock = datetime.now(timezone.utc) if now is None else now
        claims = receipt["claims"]
        audit = inspect_research_remote_reconciliation(
            task_id, receipt, ack, attempt_id, evidence, trust,
            max_input_tokens=claims["max_input_tokens_per_call"],
            max_output_tokens=claims["max_output_tokens_per_call"],
            max_total_cost_usd_micros=claims["max_total_cost_usd_micros"],
            now=clock)
        signed_claims = evidence["claims"]
        provider_id = signed_claims["provider_operation_id"]
        if (signed_claims["remote_status"] == "NOT_FOUND"
                and any(signed_claims[key] != 0 for key in (
                    "prompt_tokens", "completion_tokens", "total_tokens",
                    "charged_usd_micros"))):
            raise OrchestrationError(
                "remote NOT_FOUND must not assert observed provider billing")
        if not _PROVIDER_ID.fullmatch(provider_id):
            raise OrchestrationError("model recovery provider operation ID invalid")
        digest = fingerprint_json(evidence)
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                reserved, state = self._attempt(db, task_id, attempt_id)
                # Never convert a report into release/settlement. A charge
                # greater than the committed reservation is an incident.
                verdict = audit["status"]
                if (signed_claims["charged_usd_micros"] > reserved
                        and verdict == "REMOTE_SETTLED_SIGNATURE_VERIFIED_AUDIT_ONLY"):
                    verdict = "RECONCILIATION_REQUIRED_RESERVATION_EXCEEDED"
                op = db.execute(
                    "SELECT provider_operation_id"
                    " FROM research_model_operations WHERE attempt_id=?",
                    (attempt_id,)).fetchone()
                if op is not None and op[0] != provider_id:
                    raise OrchestrationError(
                        "remote provider operation differs from durable SDK ID")
                # A NOT_FOUND report is not proof of the operation's ID.
                if op is None and signed_claims["remote_status"] != "NOT_FOUND":
                    db.execute(
                        "INSERT INTO research_model_operations"
                        " (attempt_id,provider_operation_id,source,recorded_at)"
                        " VALUES (?,?,'SIGNED_READBACK',?)",
                        (attempt_id, provider_id, clock.astimezone(
                            timezone.utc).isoformat()))
                db.execute(
                    "INSERT INTO research_model_recovery_observations"
                    " (attempt_id,evidence_id,evidence_sha256,evidence_json,"
                    " verdict,observed_at) VALUES (?,?,?,?,?,?)",
                    (attempt_id, signed_claims["evidence_id"], digest,
                     canonical_json_bytes(evidence).decode("utf-8"),
                     verdict, clock.astimezone(timezone.utc).isoformat()))
                db.execute("COMMIT")
            except (sqlite3.Error, OrchestrationError) as exc:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise OrchestrationError(
                    "remote recovery evidence replay, drift or ledger unavailable"
                ) from exc
        return {
            "schema_version": 1, "task_id": task_id, "attempt_id": attempt_id,
            "status": "REMOTE_OBSERVATION_COMMITTED_AUDIT_ONLY",
            "reconciliation_verdict": verdict,
            "remote_status": signed_claims["remote_status"],
            "evidence_sha256": digest, "recorded_spend_state": state,
            "reserved_usd_micros": reserved,
            "provider_invoice_independently_verified": False,
            "spend_settled": False, "reservation_released": False,
            "safe_to_retry": False, "automatic_retry_allowed": False,
            "permission_to_dispatch": False,
        }
