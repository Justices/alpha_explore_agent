"""P4.27 isolated reference global model spend ledger, never an SDK dispatcher.

The service must be deployed and provisioned independently; the research
application does NOT instantiate it. Atomic reservations are held through
uncertain outcomes. No caller-supplied report can increase signed ceilings.
"""
import sqlite3
from contextlib import closing
from pathlib import Path

from alpha_research.orchestration.engine import OrchestrationError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_model_spend (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    grant_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL UNIQUE,
    reserved_usd_micros INTEGER NOT NULL,
    settled_usd_micros INTEGER,
    status TEXT NOT NULL CHECK(status IN ('RESERVED','SETTLED','UNKNOWN'))
);
CREATE INDEX IF NOT EXISTS research_model_spend_grant
    ON research_model_spend(grant_id);
"""


class ReferenceExternalSpendLedger:
    """Transactionally reserve strict budgets; no release of unknown charges."""

    def __init__(self, database_path, *, grant_id, max_model_calls,
                 max_total_cost_usd_micros):
        if (not isinstance(grant_id, str) or len(grant_id) != 32
                or any(c not in "0123456789abcdef" for c in grant_id)
                or type(max_model_calls) is not int or max_model_calls < 1
                or type(max_total_cost_usd_micros) is not int
                or max_total_cost_usd_micros < 1):
            raise OrchestrationError("external spend ledger grant budget invalid")
        self.path = Path(database_path)
        self.grant_id = grant_id
        self.max_model_calls = max_model_calls
        self.max_total_cost_usd_micros = max_total_cost_usd_micros

    def _connect(self):
        return sqlite3.connect(str(self.path), timeout=10, isolation_level=None)

    def initialize_schema(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript(_SCHEMA)

    def reserve_once(self, *, task_id, attempt_id, worst_case_usd_micros):
        if (not isinstance(task_id, str) or len(task_id) != 32
                or any(c not in "0123456789abcdef" for c in task_id)
                or not isinstance(attempt_id, str) or len(attempt_id) != 32
                or any(c not in "0123456789abcdef" for c in attempt_id)
                or type(worst_case_usd_micros) is not int
                or worst_case_usd_micros < 1
                or worst_case_usd_micros > self.max_total_cost_usd_micros):
            raise OrchestrationError("external spend reservation input invalid")
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(reserved_usd_micros),0)"
                    " FROM research_model_spend WHERE grant_id=?",
                    (self.grant_id,)).fetchone()
                if (row[0] >= self.max_model_calls
                        or row[1] + worst_case_usd_micros
                        > self.max_total_cost_usd_micros):
                    raise OrchestrationError("external model call or USD cap exhausted")
                db.execute(
                    "INSERT INTO research_model_spend"
                    " (grant_id, task_id, attempt_id, reserved_usd_micros, status)"
                    " VALUES (?, ?, ?, ?, 'RESERVED')",
                    (self.grant_id, task_id, attempt_id, worst_case_usd_micros))
                db.execute("COMMIT")
                return {
                    "status": "SPEND_RESERVED_REFERENCE_ONLY",
                    "grant_id": self.grant_id, "task_id": task_id,
                    "attempt_id": attempt_id,
                    "reserved_usd_micros": worst_case_usd_micros,
                    "provider_invoked": False,
                    "permission_to_dispatch": False,
                }
            except (sqlite3.IntegrityError, sqlite3.OperationalError,
                    OrchestrationError) as exc:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise OrchestrationError(
                    "external model spend reservation rejected or unavailable") from exc

    def mark_unknown(self, attempt_id):
        """Quarantine reservation on lost response; NEVER return capacity."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "UPDATE research_model_spend SET status='UNKNOWN'"
                " WHERE grant_id=? AND attempt_id=? AND status='RESERVED'",
                (self.grant_id, attempt_id))
            db.execute("COMMIT")
            if row.rowcount != 1:
                raise OrchestrationError("external spend attempt not reservable as unknown")
        return {"status": "UNKNOWN_REQUIRES_MANUAL_RECONCILIATION",
                "automatic_retry_allowed": False, "permission_to_dispatch": False}

    def settle_once(self, attempt_id, charged_usd_micros):
        """Settlement requires external provider evidence checked elsewhere.

        The recorded worst-case reservation is never freed or reused.
        """
        if type(charged_usd_micros) is not int or charged_usd_micros < 0:
            raise OrchestrationError("external provider charge invalid")
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT reserved_usd_micros,status FROM research_model_spend"
                    " WHERE grant_id=? AND attempt_id=?",
                    (self.grant_id, attempt_id)).fetchone()
                if (row is None or row[1] != "RESERVED"
                        or charged_usd_micros > row[0]):
                    raise OrchestrationError("external spend reconciliation requires review")
                db.execute(
                    "UPDATE research_model_spend SET status='SETTLED',"
                    " settled_usd_micros=? WHERE grant_id=? AND attempt_id=?",
                    (charged_usd_micros, self.grant_id, attempt_id))
                db.execute("COMMIT")
            except (sqlite3.OperationalError, OrchestrationError) as exc:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise OrchestrationError("external spend settlement rejected") from exc
        return {"status": "SETTLED_REFERENCE_ONLY",
                "provider_invoice_independently_verified": False,
                "automatic_retry_allowed": False,
                "permission_to_dispatch": False}

    def snapshot(self):
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT status, COUNT(*), SUM(reserved_usd_micros)"
                " FROM research_model_spend WHERE grant_id=? GROUP BY status",
                (self.grant_id,)).fetchall()
        return {
            "grant_id": self.grant_id,
            "max_model_calls": self.max_model_calls,
            "max_total_cost_usd_micros": self.max_total_cost_usd_micros,
            "reserved_attempts": sum(r[1] for r in rows),
            "committed_reservation_usd_micros": sum(r[2] for r in rows),
            "by_status": {r[0]: r[1] for r in rows},
            "remote_provider_billing_attested": False,
            "permission_to_dispatch": False,
        }
