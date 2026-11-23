"""P6.1 TLS 1.2+ mutual authentication for an isolated authority pilot.

Certificates are independently supplied by the operator. This module does
NOT provision trusted production identity, route the server on public
interfaces or authorize model/BRAIN execution.
"""
import ssl
from pathlib import Path

from alpha_research.orchestration.engine import OrchestrationError

_MAX_FILE = 32768


def _file(value):
    if not isinstance(value, (str, Path)):
        raise OrchestrationError("mTLS material path invalid")
    p = Path(value).expanduser()
    if (not p.is_absolute() or p.is_symlink()
            or not p.is_file() or not 0 < p.stat().st_size <= _MAX_FILE):
        raise OrchestrationError("mTLS material must be bounded absolute file")
    return p


def _base_context(purpose, ca_file):
    context = ssl.create_default_context(
        purpose=purpose, cafile=str(_file(ca_file)))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.MAXIMUM_SUPPORTED
    context.verify_mode = ssl.CERT_REQUIRED
    context.options |= ssl.OP_NO_COMPRESSION
    return context


def make_authority_mtls_server_context(*, certificate_file,
                                       private_key_file, client_ca_file):
    """Require a valid client cert, verified by a separately provided CA."""
    try:
        context = _base_context(ssl.Purpose.CLIENT_AUTH, client_ca_file)
        context.load_cert_chain(
            str(_file(certificate_file)), str(_file(private_key_file)))
        context.verify_mode = ssl.CERT_REQUIRED
        return context
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise OrchestrationError("mTLS authority server identity invalid") from exc


def make_authority_mtls_client_context(*, certificate_file,
                                       private_key_file, server_ca_file):
    """Require a valid server cert and hostname validation."""
    try:
        context = _base_context(ssl.Purpose.SERVER_AUTH, server_ca_file)
        context.check_hostname = True
        context.load_cert_chain(
            str(_file(certificate_file)), str(_file(private_key_file)))
        return context
    except (OSError, ssl.SSLError, ValueError) as exc:
        raise OrchestrationError("mTLS authority client identity invalid") from exc


def inspect_authority_transport_identity(*, host, server_context,
                                         client_context):
    """Static audit does not imply independent service or CA custody."""
    if (host != "127.0.0.1" or
            not isinstance(server_context, ssl.SSLContext) or
            not isinstance(client_context, ssl.SSLContext) or
            server_context.verify_mode != ssl.CERT_REQUIRED or
            client_context.verify_mode != ssl.CERT_REQUIRED or
            client_context.check_hostname is not True or
            server_context.minimum_version < ssl.TLSVersion.TLSv1_2 or
            client_context.minimum_version < ssl.TLSVersion.TLSv1_2):
        raise OrchestrationError("mTLS transport configuration not safe")
    return {
        "status": "MUTUAL_TLS_CONTEXT_INSPECTED_LOCAL_ONLY",
        "transport_encrypted": True,
        "peer_cert_required": True,
        "server_hostname_verification_required": True,
        "service_identity_independently_attested": False,
        "client_identity_independently_attested": False,
        "production_mtls_deployed": False,
        "permission_to_dispatch": False,
    }
