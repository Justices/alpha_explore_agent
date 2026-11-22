"""P4.26: driver-side session and exact read-only Catalog evidence.

This only checks a signed, Task-bound session observation. The driver
intentionally refuses live research startup until independently provisioned
admission and driver-enforced binding exist.
"""
import re

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_attestation_contract import (
    inspect_signed_research_evidence,
)
from alpha_research.orchestration.research_catalog_protocol import (
    inspect_catalog_read_contract, research_catalog_request_allowed,
)
from alpha_research.state.fingerprint import fingerprint_json

_FIELDS = frozenset({
    "scope", "source_alpha_id", "allowed_read_actions",
    "session_id", "platform_account_sha256", "driver_build_sha256",
    "grant_ack_sha256", "session_status",
})
_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def inspect_driver_session_evidence(orchestrator, task_id, receipt, ack,
                                    evidence, trust, *, now=None):
    """Verify independently signed driver observation; never open a session."""
    task = orchestrator.load(task_id)
    catalog = inspect_catalog_read_contract(orchestrator, task_id)
    source_id = (task["request"].get("alpha_id")
                 if task["workflow"] == "optimization" else None)
    expected = {
        "task_id": task_id,
        "research_receipt_sha256": fingerprint_json(receipt),
        "scope": catalog["scope"],
        "source_alpha_id": source_id,
        "allowed_read_actions": catalog["allowed_read_actions"],
        "grant_ack_sha256": fingerprint_json(ack),
        "session_status": "AUTHENTICATED_READ_ONLY",
    }
    result = inspect_signed_research_evidence(
        evidence, trust, domain="research-driver-session",
        operation="observe_research_driver_session",
        fields=_FIELDS, expected=expected, now=now)
    claims = evidence["claims"]
    if (not isinstance(claims["session_id"], str)
            or not _ID.fullmatch(claims["session_id"])
            or any(not isinstance(claims[k], str) or not _HEX64.fullmatch(claims[k])
                   for k in ("platform_account_sha256", "driver_build_sha256"))
            or (source_id is not None
                and (not isinstance(source_id, str)
                     or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", source_id) is None))):
        raise OrchestrationError("research driver session evidence identity invalid")
    return {
        **result,
        "status": "DRIVER_SESSION_SIGNATURE_VERIFIED_AUDIT_ONLY",
        "scope": catalog["scope"],
        "source_alpha_id": source_id,
        "allowed_read_actions": catalog["allowed_read_actions"],
        "driver_started": False,
        "platform_session_independently_verified": False,
        "readback_observed": False,
    }


def driver_read_frame_allowed(frame, session_report):
    """Validate a proposed frame against the exact verified audit scope.

    This pure guard does NOT authorize driver startup or transport execution.
    """
    if (not isinstance(session_report, dict)
            or session_report.get("status") !=
            "DRIVER_SESSION_SIGNATURE_VERIFIED_AUDIT_ONLY"
            or session_report.get("permission_to_dispatch") is not False
            or session_report.get("brain_read_authorized") is not False
            or not isinstance(frame, dict)
            or not isinstance(frame.get("cmd"), str)
            or frame["cmd"] not in session_report.get("allowed_read_actions", ())):
        return False
    return research_catalog_request_allowed(
        frame["cmd"], {key: value for key, value in frame.items() if key != "cmd"},
        session_report.get("scope"),
        source_alpha_id=session_report.get("source_alpha_id"))
