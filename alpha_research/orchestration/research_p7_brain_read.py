"""P7.3: Task-bound, signed-session BRAIN Catalog read admission and readback.

This is a pure offline admission audit, not BRAIN login or network dispatch.
A signed driver observation is *not* independent platform authentication.
"""
import re

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_driver_session import (
    inspect_driver_session_evidence, driver_read_frame_allowed,
)
from alpha_research.orchestration.research_p5_release import _check_monitor
from alpha_research.state.fingerprint import fingerprint_json, canonical_json_bytes

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_MAX_FRAMES = 64
_MAX_FRAME_BYTES = 16_384


def inspect_p7_brain_read_session(orchestrator, task_id, receipt, ack,
                                  signed_session, session_trust, *, frames,
                                  authority_monitor=None, now=None):
    """Verify signed session binding and every exact read-only request.

    Session evidence, Task, Grant ACK, selected scope and Optimization source
    are cross-validated by the P4.26 verifier. The P5 local Stop can veto the
    request. Even a clean result remains an *offline* frame audit.
    """
    if (not isinstance(frames, list) or not 1 <= len(frames) <= _MAX_FRAMES
            or any(not isinstance(frame, dict) for frame in frames)):
        raise OrchestrationError("P7 BRAIN requires bounded read-frame batch")
    session = inspect_driver_session_evidence(
        orchestrator, task_id, receipt, ack, signed_session, session_trust,
        now=now)
    if (not isinstance(session, dict)
            or session.get("status") != "DRIVER_SESSION_SIGNATURE_VERIFIED_AUDIT_ONLY"
            or session.get("task_id") != task_id
            or session.get("permission_to_dispatch") is not False
            or session.get("brain_read_authorized") is not False
            or session.get("platform_session_independently_verified") is not False
            or session.get("driver_started") is not False):
        raise OrchestrationError("P7 BRAIN signed-session trust contract drift")
    digests, valid = [], True
    for frame in frames:
        try:
            size = len(canonical_json_bytes(frame))
        except (ValueError, TypeError, OverflowError) as exc:
            raise OrchestrationError("P7 BRAIN read frame cannot be encoded") from exc
        if size > _MAX_FRAME_BYTES:
            raise OrchestrationError("P7 BRAIN read frame too large")
        if not driver_read_frame_allowed(frame, session):
            valid = False
        digests.append(fingerprint_json(frame))
    if len(set(digests)) != len(digests):
        raise OrchestrationError("P7 BRAIN repeated read frame in one batch")
    monitor_state = "NOT_SUPPLIED"
    if authority_monitor is not None:
        _check_monitor(authority_monitor)
        monitor_state = authority_monitor["stop_state"]
    stopped = monitor_state == "STOPPED"
    allowed = valid and monitor_state == "ARMED" and not stopped
    if not valid:
        status = "P7_BRAIN_READ_SCOPE_DENIED"
    elif stopped:
        status = "P7_BRAIN_READ_STOP_LATCHED"
    elif monitor_state == "NOT_SUPPLIED":
        status = "P7_BRAIN_READ_MONITOR_UNAVAILABLE"
    else:
        status = "P7_BRAIN_READ_FRAME_AUDITED_NO_LIVE"
    return {
        "schema_version": 1, "task_id": task_id, "status": status,
        "session_evidence_sha256": fingerprint_json(signed_session),
        "grant_ack_sha256": fingerprint_json(ack),
        "research_receipt_sha256": fingerprint_json(receipt),
        "scope": session["scope"],
        "source_alpha_id": session["source_alpha_id"],
        "frame_digests": digests,
        "frame_batch_sha256": fingerprint_json(frames),
        "frame_count": len(frames),
        "read_only_frames_valid": valid,
        "authority_stop_state": monitor_state,
        "local_offline_admission_passed": allowed,
        "platform_session_independently_authenticated": False,
        "independent_brain_read_permission_verified": False,
        "driver_started": False, "brain_read_invoked": False,
        "brain_simulation_invoked": False, "alpha_submission_invoked": False,
        "automatic_retry_allowed": False,
        "permission_to_dispatch": False,
    }


def inspect_p7_brain_readback_observation(session_report, *, frame_index,
                                         observed_response):
    """Correlate a bounded caller-provided Catalog response with one frame.

    The response envelope is not BRAIN-signed or independently read back.
    We retain only a payload digest and bounded record count; never records,
    BRAIN cookies, credentials or a false live-session success claim.
    """
    if (not isinstance(session_report, dict)
            or session_report.get("status") != "P7_BRAIN_READ_FRAME_AUDITED_NO_LIVE"
            or session_report.get("permission_to_dispatch") is not False
            or session_report.get("platform_session_independently_authenticated") is not False
            or type(frame_index) is not int
            or not 0 <= frame_index < len(session_report.get("frame_digests", ()))):
        raise OrchestrationError("P7 BRAIN readback requires audited frame")
    required = {"task_id", "frame_sha256", "scope", "http_status",
                "record_count", "payload_sha256"}
    if not isinstance(observed_response, dict) or set(observed_response) != required:
        raise OrchestrationError("P7 BRAIN response envelope invalid")
    digest = observed_response["payload_sha256"]
    if (observed_response["task_id"] != session_report["task_id"]
            or observed_response["frame_sha256"] != session_report["frame_digests"][frame_index]
            or observed_response["scope"] != session_report["scope"]
            or type(observed_response["http_status"]) is not int
            or observed_response["http_status"] != 200
            or type(observed_response["record_count"]) is not int
            or not 0 <= observed_response["record_count"] <= 10000
            or not isinstance(digest, str) or _HEX64.fullmatch(digest) is None):
        raise OrchestrationError("P7 BRAIN response/task/scope/frame mismatch")
    return {
        "schema_version": 1,
        "task_id": session_report["task_id"],
        "status": "P7_BRAIN_CALLER_REPORTED_READBACK_UNATTESTED",
        "frame_sha256": session_report["frame_digests"][frame_index],
        "payload_sha256": digest,
        "record_count": observed_response["record_count"],
        "platform_payload_authenticated": False,
        "session_authenticated_independently": False,
        "brain_read_invoked": False, "permission_to_dispatch": False,
    }
