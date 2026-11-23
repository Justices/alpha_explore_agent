"""P5.1 opt-in LOCAL PILOT launcher for the isolated research authority.

This is a standalone operator-run process. It *cannot* serve other hosts,
does not start BRAIN/LLM, and does not return live execution grants.
For a production deployment use OS secrets custody, independently operated
authentication and a reviewed TLS/mTLS proxy outside this pilot.
"""
import argparse
import json
import os
import stat
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_authority_transport import (
    ResearchAuthorityApplication, make_loopback_authority_server,
)
from alpha_research.orchestration.research_p6_mtls import (
    make_authority_mtls_server_context,
)

_CONFIG_FIELDS = {
    "schema_version", "database_file", "operator_trust_file",
    "consumer_private_key_file", "ledger_private_key_file",
    "consumer_id", "consumer_key_id", "ledger_id",
    "ledger_issuer_id", "ledger_key_id", "global_max_usd_micros",
}
_TLS_CONFIG_FIELDS = {
    "server_certificate_file", "server_private_key_file", "client_ca_file",
}


def _safe_path(raw):
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise OrchestrationError("pilot service file path invalid")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() or candidate.is_symlink():
        raise OrchestrationError("pilot service requires absolute non-symlink paths")
    return candidate


def _load_private_key(raw):
    path = _safe_path(raw)
    mode = path.stat()
    # POSIX systems require owner-only key files. Windows requires dedicated
    # ACL provisioning: this pilot does not claim to verify its ACL.
    if os.name != "nt" and (
            mode.st_uid != os.getuid()
            or stat.S_IMODE(mode.st_mode) & 0o077):
        raise OrchestrationError("pilot private key ownership/permissions unsafe")
    if mode.st_size > 16_384:
        raise OrchestrationError("pilot private key file too large")
    key = serialization.load_pem_private_key(
        path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise OrchestrationError("pilot requires Ed25519 private key")
    return key


def load_pilot_config(config_path):
    """Read non-secret config for TLS only; no automatic production enable."""
    path = _safe_path(config_path)
    if path.stat().st_size > 32_768:
        raise OrchestrationError("pilot authority config too large")
    data = json.loads(path.read_text(encoding="utf-8"))
    return data


def load_pilot_application(config_path):
    """Strict opt-in configuration; never reads a credential via CLI args."""
    config_file = _safe_path(config_path)
    if config_file.stat().st_size > 32_768:
        raise OrchestrationError("pilot authority config too large")
    cfg = json.loads(config_file.read_text(encoding="utf-8"))
    if (not isinstance(cfg, dict)
            or set(cfg) not in (
                _CONFIG_FIELDS,
                _CONFIG_FIELDS | {"recovery_trust_file"},
                _CONFIG_FIELDS | _TLS_CONFIG_FIELDS,
                _CONFIG_FIELDS | _TLS_CONFIG_FIELDS | {"recovery_trust_file"})
            or type(cfg["schema_version"]) is not int
            or cfg["schema_version"] != 1):
        raise OrchestrationError("pilot authority config schema invalid")
    trust_path = _safe_path(cfg["operator_trust_file"])
    if trust_path.stat().st_size > 65_536:
        raise OrchestrationError("pilot operator trust file too large")
    trust = json.loads(trust_path.read_text(encoding="utf-8"))
    recovery_trust = None
    if "recovery_trust_file" in cfg:
        recovery_path = _safe_path(cfg["recovery_trust_file"])
        if recovery_path.stat().st_size > 65_536:
            raise OrchestrationError("pilot recovery trust file too large")
        recovery_trust = json.loads(recovery_path.read_text(encoding="utf-8"))
    database = _safe_path(cfg["database_file"])
    if database.exists() and not database.is_file():
        raise OrchestrationError("pilot authority database path invalid")
    return ResearchAuthorityApplication(
        database,
        consumer_signer=_load_private_key(cfg["consumer_private_key_file"]),
        ledger_signer=_load_private_key(cfg["ledger_private_key_file"]),
        operator_trust=trust,
        consumer_id=cfg["consumer_id"],
        consumer_key_id=cfg["consumer_key_id"],
        ledger_id=cfg["ledger_id"],
        ledger_issuer_id=cfg["ledger_issuer_id"],
        ledger_key_id=cfg["ledger_key_id"],
        global_max_usd_micros=cfg["global_max_usd_micros"],
        recovery_trust=recovery_trust,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Isolated read-only research authority LOCAL PILOT; optional mTLS; no live dispatch")
    parser.add_argument("--config", required=True,
                        help="absolute path to non-secret pilot configuration JSON")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--enable-local-pilot", action="store_true",
                        help="explicitly run loopback-only service, never production")
    opts = parser.parse_args(argv)
    if not opts.enable_local_pilot:
        parser.error("--enable-local-pilot is required")
    if opts.port < 1 or opts.port > 65535:
        parser.error("pilot port must be 1..65535")
    try:
        api = os.environ.get("ALPHA_RESEARCH_AUTHORITY_API_TOKEN", "")
        admin = os.environ.get("ALPHA_RESEARCH_AUTHORITY_ADMIN_TOKEN", "")
        application = load_pilot_application(opts.config)
        config = load_pilot_config(opts.config)
        tls = (make_authority_mtls_server_context(
            certificate_file=config["server_certificate_file"],
            private_key_file=config["server_private_key_file"],
            client_ca_file=config["client_ca_file"])
            if _TLS_CONFIG_FIELDS.issubset(config) else None)
        server = make_loopback_authority_server(
            application, api_token=api, admin_token=admin,
            port=opts.port, tls_context=tls)
        try:
            application.initialize_schema()
            print("Research authority LOCAL PILOT: 127.0.0.1:"
                  + str(server.server_address[1]) + "; no live dispatch",
                  flush=True)
            try:
                server.serve_forever(poll_interval=0.2)
            except KeyboardInterrupt:
                pass
        finally:
            server.server_close()
    except Exception:
        # Intentionally no exception traceback or private file/credential data.
        print("Research authority local pilot refused startup", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
