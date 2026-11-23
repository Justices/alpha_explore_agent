"""P4.27 isolated reference global model spend ledger, never an SDK dispatcher.

The service must be deployed and provisioned independently; the research
application does NOT instantiate it. Atomic reservations are held through
uncertain outcomes. No caller-supplied report can increase signed ceilings.
"""
import json
import sqlite3
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_only_admission import _utc
from alpha_research.state.fingerprint import fingerprint_json
from alpha_research.orchestration.research_canary_control_reference import (
    assert_research_canary_not_stopped,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_model_spend_limits (
    scope_id TEXT PRIMARY KEY,
    max_model_calls INTEGER NOT NULL,
    max_usd_micros INTEGER NOT NULL
);
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
                 max_total_cost_usd_micros,
                 global_max_total_cost_usd_micros,
                 require_canary_arm=False):
        if (not isinstance(grant_id, str) or len(grant_id) != 32
                or any(c not in "0123456789abcdef" for c in grant_id)
                or type(max_model_calls) is not int or max_model_calls < 1
                or type(max_total_cost_usd_micros) is not int
                or max_total_cost_usd_micros < 1
                or type(global_max_total_cost_usd_micros) is not int
                or global_max_total_cost_usd_micros < 1
                or type(require_canary_arm) is not bool):
            raise OrchestrationError("external spend ledger grant budget invalid")
        self.path = Path(database_path)
        self.grant_id = grant_id
        self.max_model_calls = max_model_calls
        self.max_total_cost_usd_micros = max_total_cost_usd_micros
        self.global_max_total_cost_usd_micros = global_max_total_cost_usd_micros
        self.require_canary_arm = require_canary_arm

    def _connect(self):
        return sqlite3.connect(str(self.path), timeout=10, isolation_level=None)

    def initialize_schema(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript(_SCHEMA)
            try:
                db.execute("BEGIN IMMEDIATE")
                for scope, calls, cap in (
                    ("GLOBAL", 0, self.global_max_total_cost_usd_micros),
                    (self.grant_id, self.max_model_calls,
                     self.max_total_cost_usd_micros),
                ):
                    db.execute(
                        "INSERT OR IGNORE INTO research_model_spend_limits"
                        " (scope_id, max_model_calls, max_usd_micros)"
                        " VALUES (?, ?, ?)", (scope, calls, cap))
                self._verify_limits(db)
                db.execute("COMMIT")
            except (sqlite3.OperationalError, OrchestrationError) as exc:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise OrchestrationError(
                    "external spend ledger immutable policy conflict") from exc

    def _verify_limits(self, db):
        global_row = db.execute(
            "SELECT max_model_calls,max_usd_micros"
            " FROM research_model_spend_limits WHERE scope_id='GLOBAL'").fetchone()
        grant_row = db.execute(
            "SELECT max_model_calls,max_usd_micros"
            " FROM research_model_spend_limits WHERE scope_id=?",
            (self.grant_id,)).fetchone()
        if (global_row != (0, self.global_max_total_cost_usd_micros)
                or grant_row != (self.max_model_calls,
                                 self.max_total_cost_usd_micros)):
            raise OrchestrationError(
                "external spend ledger limits differ from persisted policy")

    def reserve_once(self, *, task_id, attempt_id, worst_case_usd_micros,
                     expected_grant_ack_sha256=None):
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
                self._verify_limits(db)
                if self.require_canary_arm:
                    assert_research_canary_not_stopped(db)
                if expected_grant_ack_sha256 is not None:
                    if (not isinstance(expected_grant_ack_sha256, str)
                            or len(expected_grant_ack_sha256) != 64
                            or any(c not in "0123456789abcdef"
                                   for c in expected_grant_ack_sha256)):
                        raise OrchestrationError("spend reservation ACK digest invalid")
                    grant = db.execute(
                        "SELECT receipt_sha256,receipt_json,ack_json"
                        " FROM research_grant_consumptions WHERE grant_id=?",
                        (self.grant_id,)).fetchone()
                    revoked = db.execute(
                        "SELECT 1 FROM research_grant_revocations WHERE grant_id=?",
                        (self.grant_id,)).fetchone()
                    if not grant or revoked or not grant[2]:
                        raise OrchestrationError("grant not consumed or revoked")
                    ack = json.loads(grant[2])
                    receipt = json.loads(grant[1])
                    if (fingerprint_json(ack) != expected_grant_ack_sha256
                            or fingerprint_json(receipt) != grant[0]
                            or ack["claims"]["task_id"] != task_id
                            or ack["claims"]["grant_id"] != self.grant_id
                            or ack["claims"]["research_receipt_sha256"] != grant[0]
                            or _utc(ack["claims"]["expires_at"])
                               <= datetime.now(timezone.utc)):
                        raise OrchestrationError("grant ACK expired or scope mismatch")
                row = db.execute(
                    "SELECT COUNT(*), COALESCE(SUM(reserved_usd_micros),0)"
                    " FROM research_model_spend WHERE grant_id=?",
                    (self.grant_id,)).fetchone()
                global_committed = db.execute(
                    "SELECT COALESCE(SUM(reserved_usd_micros),0)"
                    " FROM research_model_spend").fetchone()[0]
                if (row[0] >= self.max_model_calls
                        or row[1] + worst_case_usd_micros
                        > self.max_total_cost_usd_micros
                        or global_committed + worst_case_usd_micros
                        > self.global_max_total_cost_usd_micros):
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

    def mark_unknown(self, attempt_id, *, task_id=None):
        """Quarantine exact reserved attempt; revoked Grants can still quarantine.

        The operation is transition-once, and never returns reserved capacity.
        Pilot transport must supply a Task ID to prevent cross-Task edits.
        """
        if (not isinstance(attempt_id, str) or len(attempt_id) != 32
                or any(c not in "0123456789abcdef" for c in attempt_id)
                or (task_id is not None and (
                    not isinstance(task_id, str) or len(task_id) != 32
                    or any(c not in "0123456789abcdef" for c in task_id)))):
            raise OrchestrationError("external spend quarantine identity invalid")
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                self._verify_limits(db)
                if task_id is None:
                    row = db.execute(
                        "UPDATE research_model_spend SET status='UNKNOWN'"
                        " WHERE grant_id=? AND attempt_id=? AND status='RESERVED'",
                        (self.grant_id, attempt_id))
                else:
                    row = db.execute(
                        "UPDATE research_model_spend SET status='UNKNOWN'"
                        " WHERE grant_id=? AND task_id=? AND attempt_id=?"
                        " AND status='RESERVED'",
                        (self.grant_id, task_id, attempt_id))
                if row.rowcount != 1:
                    raise OrchestrationError(
                        "external spend attempt not reservable as unknown")
                db.execute("COMMIT")
            except (sqlite3.Error, OrchestrationError) as exc:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise OrchestrationError(
                    "external spend quarantine failed closed") from exc
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
                self._verify_limits(db)
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
            global_committed = db.execute(
                "SELECT COALESCE(SUM(reserved_usd_micros),0)"
                " FROM research_model_spend").fetchone()[0]
            rows = db.execute(
                "SELECT status, COUNT(*), SUM(reserved_usd_micros)"
                " FROM research_model_spend WHERE grant_id=? GROUP BY status",
                (self.grant_id,)).fetchall()
        return {
            "grant_id": self.grant_id,
            "max_model_calls": self.max_model_calls,
            "max_total_cost_usd_micros": self.max_total_cost_usd_micros,
            "global_max_total_cost_usd_micros": self.global_max_total_cost_usd_micros,
            "global_committed_reservation_usd_micros": global_committed,
            "reserved_attempts": sum(r[1] for r in rows),
            "committed_reservation_usd_micros": sum(r[2] for r in rows),
            "by_status": {r[0]: r[1] for r in rows},
            "remote_provider_billing_attested": False,
            "permission_to_dispatch": False,
        }
