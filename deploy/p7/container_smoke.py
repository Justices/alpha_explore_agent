"""End-to-end P7 container acceptance with disposable offline-only credentials.

Linux Docker host network is required. No Provider/BRAIN credentials or API
calls, no production deployment, no paid operations. Run only in CI/operator
sandbox with a local image built from deploy/p7/Dockerfile.
"""
import http.client
import ipaddress
import json
import os
import secrets
import ssl
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

_IMAGE = "alpha-research-authority:p7-ci"


def _write(path, data):
    path.write_bytes(data)
    path.chmod(0o600)


def _pem_key(key):
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())


def _fixture_pkix(private, client):
    now = datetime.now(timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "P7 disposable CI CA")
    ])
    ca = (x509.CertificateBuilder()
          .subject_name(name).issuer_name(name)
          .public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(minutes=5))
          .not_valid_after(now + timedelta(hours=2))
          .add_extension(
              x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(
              x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
              critical=False)
          .add_extension(
              x509.AuthorityKeyIdentifier.from_issuer_public_key(
                  ca_key.public_key()), critical=False)
          .add_extension(
              x509.KeyUsage(
                  digital_signature=False, content_commitment=False,
                  key_encipherment=False, data_encipherment=False,
                  key_agreement=False, key_cert_sign=True, crl_sign=True,
                  encipher_only=False, decipher_only=False),
              critical=True)
          .sign(ca_key, hashes.SHA256()))

    def leaf(common_name, usage, *, ip_san=False):
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, common_name)
        ])
        builder = (x509.CertificateBuilder()
                   .subject_name(subject).issuer_name(ca.subject)
                   .public_key(key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(minutes=5))
                   .not_valid_after(now + timedelta(hours=1))
                   .add_extension(
                       x509.BasicConstraints(ca=False, path_length=None),
                       critical=True)
                   .add_extension(
                       x509.SubjectKeyIdentifier.from_public_key(
                           key.public_key()), critical=False)
                   .add_extension(
                       x509.AuthorityKeyIdentifier.from_issuer_public_key(
                           ca_key.public_key()), critical=False)
                   .add_extension(
                       x509.ExtendedKeyUsage([usage]), critical=False)
                   .add_extension(
                       x509.KeyUsage(
                           digital_signature=True, content_commitment=False,
                           key_encipherment=False, data_encipherment=False,
                           key_agreement=False, key_cert_sign=False,
                           crl_sign=False, encipher_only=False,
                           decipher_only=False), critical=True))
        if ip_san:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                critical=False)
        return builder.sign(ca_key, hashes.SHA256()), key

    server_cert, server_key = leaf(
        "p7-server", ExtendedKeyUsageOID.SERVER_AUTH, ip_san=True)
    client_cert, client_key = leaf(
        "p7-client", ExtendedKeyUsageOID.CLIENT_AUTH)

    _write(private / "server.crt", server_cert.public_bytes(
        serialization.Encoding.PEM))
    _write(private / "server.key", _pem_key(server_key))
    _write(private / "client-ca.crt", ca.public_bytes(
        serialization.Encoding.PEM))
    _write(client / "client.crt", client_cert.public_bytes(
        serialization.Encoding.PEM))
    _write(client / "client.key", _pem_key(client_key))
    _write(client / "server-ca.crt", ca.public_bytes(
        serialization.Encoding.PEM))


def _provision(root):
    private, state, client = (
        root / "private", root / "state", root / "client")
    for p in (private, state, client):
        p.mkdir(mode=0o700)
    _fixture_pkix(private, client)
    for name in ("consumer.pem", "ledger.pem"):
        _write(private / name, _pem_key(ed25519.Ed25519PrivateKey.generate()))
    (private / "operator-trust.json").write_text(
        json.dumps({"schema_version": 1, "revoked_grants": [],
                    "issuers": {}}), encoding="utf-8")
    api, admin = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
    for folder in (private, client):
        _write(folder / "api-token", (api + "\n").encode("ascii"))
        _write(folder / "admin-token", (admin + "\n").encode("ascii"))
    config = {
        "schema_version": 1,
        "database_file": "/var/lib/alpha-research/authority.sqlite3",
        "operator_trust_file": "/run/alpha-research/operator-trust.json",
        "consumer_private_key_file": "/run/alpha-research/consumer.pem",
        "ledger_private_key_file": "/run/alpha-research/ledger.pem",
        "consumer_id": "consumer", "consumer_key_id": "consumer-key",
        "ledger_id": "ledger", "ledger_issuer_id": "ledger-issuer",
        "ledger_key_id": "ledger-key", "global_max_usd_micros": 10000,
        "server_certificate_file": "/run/alpha-research/server.crt",
        "server_private_key_file": "/run/alpha-research/server.key",
        "client_ca_file": "/run/alpha-research/client-ca.crt",
    }
    _write(private / "config.json", json.dumps(config).encode("utf-8"))
    # The container process is UID 65532; keep host test client owned by
    # the CI user, but chown only its private mount + state volume.
    subprocess.run([
        "sudo", "chown", "-R", "65532:65532", str(private), str(state)
    ], check=True, stdout=subprocess.DEVNULL)
    return private, state, client


def _docker(args, *, capture=False):
    return subprocess.run(
        ["docker", *args], check=True, text=True,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture else None)


def _start(name, private, state):
    _docker([
        "run", "--detach", "--name", name, "--network", "host",
        "--read-only", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "64", "--user", "65532:65532",
        "--init",
        "-e", "ALPHA_RESEARCH_AUTHORITY_API_TOKEN_FILE=/run/alpha-research/api-token",
        "-e", "ALPHA_RESEARCH_AUTHORITY_ADMIN_TOKEN_FILE=/run/alpha-research/admin-token",
        "-v", f"{private}:/run/alpha-research:ro",
        "-v", f"{state}:/var/lib/alpha-research",
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=16m",
        _IMAGE,
    ])


def _request(client_dir, token_file, route):
    context = ssl.create_default_context(
        cafile=str(client_dir / "server-ca.crt"))
    context.load_cert_chain(
        certfile=str(client_dir / "client.crt"),
        keyfile=str(client_dir / "client.key"))
    token = (client_dir / token_file).read_text(encoding="ascii").strip()
    connection = http.client.HTTPSConnection(
        "127.0.0.1", 8765, timeout=2, context=context)
    try:
        connection.request("POST", route, body=b"{}",
                           headers={
                               "Content-Type": "application/json",
                               "Authorization": "Bearer " + token,
                           })
        result = connection.getresponse()
        content = result.read(65537)
        if result.status != 200 or len(content) > 65536:
            raise RuntimeError("isolated authority monitor/stop refused")
        record = json.loads(content)
        if record.get("permission_to_dispatch") is not False:
            raise RuntimeError("isolated authority returned positive dispatch")
        return record
    finally:
        connection.close()


def _check_readiness(name, expected_state):
    result = _docker([
        "exec", name, "python", "-m",
        "alpha_research.orchestration.research_authority_server",
        "--config", "/run/alpha-research/config.json", "--port", "8765",
        "--require-mtls", "--require-file-secrets", "--check-ready",
    ], capture=True)
    data = json.loads(result.stdout)
    if (data.get("ready") is not True
            or data.get("stop_state") != expected_state
            or data.get("permission_to_dispatch") is not False):
        raise RuntimeError("P7 readiness probe violated isolated contract")


def _await_monitor(client, expected, *, attempts=20):
    for _ in range(attempts):
        try:
            record = _request(client, "api-token", "/v1/canary/monitor")
            if record.get("stop_state") == expected:
                return record
        except (OSError, ssl.SSLError, RuntimeError, ValueError):
            pass  # Read-only monitor is idempotent; never retry Stop.
        time.sleep(0.5)
    raise RuntimeError("isolated P7 authority never became read-only reachable")


def main():
    if sys.platform != "linux":
        raise RuntimeError("P7 Docker host-network smoke is Linux-only")
    with tempfile.TemporaryDirectory(prefix="alpha-p7-container-") as base:
        root = Path(base)
        root.chmod(0o755)
        private, state, client = _provision(root)
        name = f"alpha-research-p7-ci-{os.getpid()}"
        try:
            _start(name, private, state)
            _await_monitor(client, "ARMED")
            _check_readiness(name, "ARMED")
            stopped = _request(client, "admin-token", "/v1/canary/stop")
            if stopped.get("stop_state") != "STOPPED":
                raise RuntimeError("P7 admin Stop was not durably latched")
            _await_monitor(client, "STOPPED")
            _check_readiness(name, "STOPPED")
            _docker(["rm", "-f", name])
            _start(name, private, state)
            _await_monitor(client, "STOPPED")
            _check_readiness(name, "STOPPED")
            print("P7_OFFLINE_CONTAINER_MTLS_STOP_RESTART_VERIFIED")
        finally:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False)
            # Private fixture files were deliberately owned by container UID
            # 65532; restore CI runner ownership before TemporaryDirectory
            # performs its automatic secure cleanup.
            subprocess.run([
                "sudo", "chown", "-R",
                f"{os.getuid()}:{os.getgid()}",
                str(private), str(state),
            ], check=True, stdout=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
