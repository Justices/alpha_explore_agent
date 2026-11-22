"""P4.25: signed second-source global-grant ledger readback audit.

The existing consumer ACK is only an assertion by its issuer. A separately
signed ledger readback adds a second observation, but is NOT proof of atomic
single-use consumption, external service availability or key custody.
"""
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_attestation_contract import (
    inspect_signed_research_evidence,
)
from alpha_research.orchestration.research_global_consumer import (
    verify_global_consumption_ack,
)
from alpha_research.state.fingerprint import fingerprint_json

_FIELDS = frozenset({
    "grant_id", "consumption_id", "ack_sha256", "ledger_id",
    "ledger_sequence", "consumer_id", "remote_grant_state", "readback_source",
})


def inspect_research_grant_ledger(
        orchestrator, task_id, model_config, governance_policy,
        receipt, operator_trust, consumer_ack, consumer_trust,
        ledger_readback, ledger_trust, *, now=None):
    """Correlate signed ACK to a separately signed immutable ledger observation."""
    verified = verify_global_consumption_ack(
        orchestrator, task_id, model_config, governance_policy,
        receipt, operator_trust, consumer_ack, consumer_trust, now=now)
    ack = consumer_ack["claims"]
    expected = {
        "task_id": task_id,
        "research_receipt_sha256": fingerprint_json(receipt),
        "grant_id": verified["grant_id"],
        "consumption_id": verified["consumption_id"],
        "ack_sha256": verified["ack_sha256"],
        "ledger_id": ack["ledger_id"],
        "ledger_sequence": ack["ledger_sequence"],
        "consumer_id": ack["consumer_id"],
        "remote_grant_state": "CONSUMED_ONCE",
        "readback_source": "REMOTE_LEDGER",
    }
    result = inspect_signed_research_evidence(
        ledger_readback, ledger_trust,
        domain="research-grant-ledger", operation="read_research_grant_ledger",
        fields=_FIELDS, expected=expected, now=now)
    # Reject the most direct self-attestation: the same consumer verification
    # key must not masquerade as a separate ledger readback verification key.
    ledger_claims = ledger_readback["claims"]
    ledger_entry = ledger_trust["issuers"][ledger_claims["issuer_id"]]
    consumer_entry = consumer_trust["consumers"][ack["consumer_id"]]
    if ledger_entry["public_key_b64"] == consumer_entry["public_key_b64"]:
        raise OrchestrationError("ledger readback must use a distinct verification key")
    return {
        **result,
        "status": "GLOBAL_LEDGER_READBACK_SIGNATURE_VERIFIED_AUDIT_ONLY",
        "grant_id": verified["grant_id"],
        "consumption_id": verified["consumption_id"],
        "ack_sha256": verified["ack_sha256"],
        "remote_ledger_observed": False,
        "atomic_single_use_proven": False,
        "live_grant_authorized": False,
    }
