"""P7 isolated authority deployment prerequisites and read-only status probe.

This module cannot authorize an SDK request or BRAIN operation. A green local
probe says only that the local audit/recovery database and operator-supplied
transport materials can be read. It is not independent service attestation.
"""
import os
import re
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_p6_mtls import (
    make_authority_mtls_server_context,
)

_API_ENV = "ALPHA_RESEARCH_AUTHORITY_API_TOKEN"
_ADMIN_ENV = "ALPHA_RESEARCH_AUTHORITY_ADMIN_TOKEN"
_API_FILE_ENV = "ALPHA_RESEARCH_AUTHORITY_API_TOKEN_FILE"
_ADMIN_FILE_ENV = "ALPHA_RESEARCH_AUTHORITY_ADMIN_TOKEN_FILE"
_TLS_FIELDS = frozenset({
    "server_certificate_file", "server_private_key_file", "client_ca_file",
})
_BEARER = re.compile(r"[A-Za-z0-9_-]{32,512}\Z")
_TABLES = frozenset({
    "research_grant_consumptions", "research_grant_revocations",
    "research_canary_control", "research_canary_stop_events",
    "research_model_recovery_observations",
})


def _secure_file(path, *, maximum=4096):
    if not isinstance(path, str) or not path or "\x00" in path:
        raise OrchestrationError("authority secret path invalid")
    p = Path(path).expanduser()
    if not p.is_absolute() or p.is_symlink() or not p.is_file():
        raise OrchestrationError("authority secret file must be an absolute regular file")
    entry = p.stat()
    if not 0 < entry.st_size <= maximum:
        raise OrchestrationError("authority secret file unavailable or oversized")
    if os.name != "nt" and (
            entry.st_uid != os.getuid()
            or stat.S_IMODE(entry.st_mode) & 0o077):
        raise OrchestrationError("authority secret file owner/permissions unsafe")
    return p


def _load_token_from_env(environ, *, token_env, file_env, file_required):
    inline = environ.get(token_env)
    path = environ.get(file_env)
    if (inline is not None and path is not None
            or file_required and path is None):
        raise OrchestrationError("authority secret source ambiguous or missing")
    if path is not None:
        try:
            value = _secure_file(path).read_text(encoding="ascii")
        except (OSError, UnicodeError) as exc:
            raise OrchestrationError("authority secret file unreadable") from exc
        # Only one conventional final line terminator may be accepted.
        value = value.removesuffix("\n").removesuffix("\r")
    else:
        value = inline
    if not isinstance(value, str) or not _BEARER.fullmatch(value):
        raise OrchestrationError("authority secret invalid")
    return value


def load_authority_transport_secrets(environ=None, *, require_files=False):
    """Load independent Bearer roles. Never print or return a diagnostic key."""
    if type(require_files) is not bool:
        raise OrchestrationError("authority secret mode invalid")
    source = os.environ if environ is None else environ
    api = _load_token_from_env(
        source, token_env=_API_ENV, file_env=_API_FILE_ENV,
        file_required=require_files)
    admin = _load_token_from_env(
        source, token_env=_ADMIN_ENV, file_env=_ADMIN_FILE_ENV,
        file_required=require_files)
    if api == admin:
        raise OrchestrationError("authority API and admin tokens must be separate")
    return api, admin


def make_protected_authority_mtls_context(config):
    """P7 deployment-only context with protected private-key custody."""
    if not isinstance(config, dict) or not _TLS_FIELDS.issubset(config):
        raise OrchestrationError("authority mTLS configuration incomplete")
    _secure_file(config["server_private_key_file"], maximum=32_768)
    return make_authority_mtls_server_context(
        certificate_file=config["server_certificate_file"],
        private_key_file=config["server_private_key_file"],
        client_ca_file=config["client_ca_file"])


def inspect_local_authority_deployment(config, *, require_mtls=False,
                                       require_file_secrets=False,
                                       environ=None):
    """No networking, writes, or key exposure: inspect local recovery readiness.

    'ready' is deliberately only local service availability, NOT authorization
    to dispatch a model, perform BRAIN reads, simulate or submit any Alpha.
    """
    if (not isinstance(config, dict)
            or type(require_mtls) is not bool
            or type(require_file_secrets) is not bool):
        raise OrchestrationError("authority deployment inspection input invalid")
    # Validate custody/config without returning any secret material.
    load_authority_transport_secrets(
        environ, require_files=require_file_secrets)
    has_tls = _TLS_FIELDS.issubset(config)
    if require_mtls and not has_tls:
        raise OrchestrationError("isolated service requires all mTLS materials")
    if has_tls:
        make_protected_authority_mtls_context(config)
    db_path = config.get("database_file")
    if not isinstance(db_path, str) or not Path(db_path).is_absolute():
        raise OrchestrationError("authority database path invalid")
    database = Path(db_path)
    ready = False
    stop_state = "UNKNOWN"
    spend_initialized = False
    reason = "LOCAL_RECOVERY_STORE_UNAVAILABLE"
    if database.is_file() and not database.is_symlink():
        try:
            # SQLite mode=ro never initializes or mutates missing state.
            uri = "file:" + quote(str(database), safe="/") + "?mode=ro"
            with closing(sqlite3.connect(
                    uri, uri=True, isolation_level=None, timeout=2)) as db:
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                check = db.execute("PRAGMA quick_check").fetchone()
                tables = {name for (name,) in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                row = (db.execute(
                    "SELECT state,event_id,stopped_at"
                    " FROM research_canary_control WHERE scope_id='GLOBAL'"
                ).fetchone() if _TABLES.issubset(tables) else None)
                budget_table = "research_model_spend_limits" in tables
                limit = (db.execute(
                    "SELECT max_usd_micros FROM research_model_spend_limits"
                    " WHERE scope_id='GLOBAL'"
                ).fetchone() if budget_table else None)
                db.execute("COMMIT")
            valid_armed = row == ("ARMED", None, None)
            valid_stopped = (
                row is not None
                and row[0] == "STOPPED"
                and isinstance(row[1], str) and bool(row[1])
                and isinstance(row[2], str) and bool(row[2]))
            if check == ("ok",) and (valid_armed or valid_stopped):
                stop_state = row[0]
                ready = True
                reason = ("LOCAL_AUDIT_ONLY"
                          if stop_state == "ARMED"
                          else "LOCAL_RECOVERY_ONLY_STOP_LATCHED")
                spend_initialized = (
                    limit is not None
                    and type(limit[0]) is int
                    and limit[0] == config.get("global_max_usd_micros"))
            elif check != ("ok",):
                reason = "LOCAL_RECOVERY_STORE_INTEGRITY_FAILED"
            else:
                reason = "LOCAL_RECOVERY_SCHEMA_OR_STOP_INVALID"
        except (OSError, sqlite3.Error, ValueError, TypeError):
            reason = "LOCAL_RECOVERY_STORE_UNAVAILABLE"
    return {
        "schema_version": 1,
        "status": reason,
        "ready": ready,
        "stop_state": stop_state,
        "global_budget_policy_initialized": spend_initialized,
        "transport_materials_checked": has_tls,
        "external_service_independently_attested": False,
        "provider_invoice_independently_verified": False,
        "brain_read_authorized": False,
        "model_call_authorized": False,
        "brain_simulation_authorized": False,
        "alpha_submission_authorized": False,
        "permission_to_dispatch": False,
    }
