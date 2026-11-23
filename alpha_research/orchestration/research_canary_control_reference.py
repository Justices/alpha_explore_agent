"""P5.5 independent local-pilot emergency stop and passive telemetry.

Stop is GLOBAL, durable, latched, and never reset by an API. Monitoring
exposes no keys, prompts, receipts or provider content. No real dispatch.
"""
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from alpha_research.orchestration.engine import OrchestrationError

CANARY_CONTROL_SQL = """
CREATE TABLE IF NOT EXISTS research_canary_control (
    scope_id TEXT PRIMARY KEY CHECK(scope_id='GLOBAL'),
    state TEXT NOT NULL CHECK(state IN ('ARMED','STOPPED')),
    event_id TEXT,
    stopped_at TEXT
);
CREATE TABLE IF NOT EXISTS research_canary_stop_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    scope_id TEXT NOT NULL CHECK(scope_id='GLOBAL'),
    stopped_at TEXT NOT NULL
);
"""


def assert_research_canary_not_stopped(db):
    """Must be invoked *inside* a caller's BEGIN IMMEDIATE transaction."""
    row = db.execute(
        "SELECT state,event_id,stopped_at FROM research_canary_control"
        " WHERE scope_id='GLOBAL'").fetchone()
    if row != ("ARMED", None, None):
        raise OrchestrationError("research independent canary stop is not armed")


class ReferenceResearchCanaryControl:
    """Reference-only operator control; no reset, dispatch, or SDK access."""

    def __init__(self, database_path):
        self.path = Path(database_path)

    def _connect(self):
        return sqlite3.connect(str(self.path), timeout=10, isolation_level=None)

    def initialize_schema(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db:
            db.executescript(CANARY_CONTROL_SQL)
            try:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "INSERT OR IGNORE INTO research_canary_control"
                    " (scope_id,state,event_id,stopped_at)"
                    " VALUES ('GLOBAL','ARMED',NULL,NULL)")
                row = db.execute(
                    "SELECT state,event_id,stopped_at"
                    " FROM research_canary_control WHERE scope_id='GLOBAL'"
                ).fetchone()
                if (row not in (("ARMED", None, None),)
                        and not (row and row[0] == "STOPPED"
                                 and isinstance(row[1], str)
                                 and isinstance(row[2], str))):
                    raise OrchestrationError("research stop record corrupted")
                db.execute("COMMIT")
            except (sqlite3.Error, OrchestrationError) as exc:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise OrchestrationError(
                    "research stop initial state unavailable") from exc

    def trigger_stop(self):
        """Irreversibly latch stop, globally serialized with budget writes."""
        event_id = secrets.token_hex(16)
        stopped_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "UPDATE research_canary_control"
                    " SET state='STOPPED',event_id=?,stopped_at=?"
                    " WHERE scope_id='GLOBAL' AND state='ARMED'"
                    " AND event_id IS NULL AND stopped_at IS NULL",
                    (event_id, stopped_at))
                if row.rowcount != 1:
                    raise OrchestrationError("research canary already stopped")
                db.execute(
                    "INSERT INTO research_canary_stop_events"
                    " (event_id,scope_id,stopped_at)"
                    " VALUES (?,'GLOBAL',?)", (event_id, stopped_at))
                db.execute("COMMIT")
            except (sqlite3.Error, OrchestrationError) as exc:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise OrchestrationError(
                    "research canary stop unavailable or already latched"
                ) from exc
        return {
            "schema_version": 1, "status": "CANARY_GLOBAL_STOP_LATCHED",
            "stop_state": "STOPPED", "event_id": event_id,
            "stopped_at": stopped_at, "permission_to_dispatch": False,
            "automatic_retry_allowed": False,
        }

    def snapshot(self):
        """Passive counters + global stop. All spend is *committed reserve*."""
        with closing(self._connect()) as db:
            try:
                db.execute("BEGIN")
                control = db.execute(
                    "SELECT state,event_id,stopped_at"
                    " FROM research_canary_control WHERE scope_id='GLOBAL'"
                ).fetchone()
                if (not control or control[0] not in ("ARMED", "STOPPED")
                        or (control[0] == "ARMED"
                            and control[1:] != (None, None))
                        or (control[0] == "STOPPED"
                            and (not control[1] or not control[2]))):
                    raise OrchestrationError("research canary control invalid")
                spend = db.execute(
                    "SELECT status, COUNT(*), COALESCE(SUM(reserved_usd_micros),0)"
                    " FROM research_model_spend GROUP BY status"
                ).fetchall()
                grants = db.execute(
                    "SELECT COUNT(*) FROM research_grant_consumptions"
                ).fetchone()[0]
                revocations = db.execute(
                    "SELECT COUNT(*) FROM research_grant_revocations"
                ).fetchone()[0]
                observations = db.execute(
                    "SELECT COUNT(*) FROM research_model_recovery_observations"
                ).fetchone()[0]
                limit = db.execute(
                    "SELECT max_usd_micros FROM research_model_spend_limits"
                    " WHERE scope_id='GLOBAL'"
                ).fetchone()
                if limit is None:
                    raise OrchestrationError("research global spend policy missing")
                db.execute("COMMIT")
            except (sqlite3.Error, OrchestrationError) as exc:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise OrchestrationError(
                    "research canary telemetry unavailable") from exc
        by_state = {state: count for state, count, _ in spend}
        committed = sum(amount for _, _, amount in spend)
        cap = limit[0]
        issues = []
        if control[0] == "STOPPED":
            issues.append("GLOBAL_STOP_LATCHED")
        if by_state.get("UNKNOWN", 0):
            issues.append("UNRESOLVED_PROVIDER_ATTEMPTS")
        if committed * 5 >= cap * 4:
            issues.append("GLOBAL_RESERVED_BUDGET_AT_LEAST_80_PERCENT")
        return {
            "schema_version": 1,
            "status": "CANARY_LOCAL_TELEMETRY_AUDIT_ONLY",
            "stop_state": control[0],
            "stop_event_id": control[1],
            "stop_time": control[2],
            "consumed_grants": grants,
            "revoked_grants": revocations,
            "attempts_by_status": by_state,
            "total_attempts": sum(by_state.values()),
            "total_committed_usd_micros": committed,
            "global_cap_usd_micros": cap,
            "signed_recovery_observations": observations,
            "alerts": issues,
            "provider_invoice_independently_verified": False,
            "independent_service_identity_attested": False,
            "real_canary_authorized": False,
            "permission_to_dispatch": False,
            "automatic_retry_allowed": False,
        }
