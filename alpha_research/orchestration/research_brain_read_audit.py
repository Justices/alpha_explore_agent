"""Pure BRAIN Catalog frame policy check; never starts a BRAIN session."""
from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_catalog_protocol import (
    inspect_catalog_read_contract, research_catalog_request_allowed,
)

def inspect_p6_brain_catalog_frame(orchestrator, task_id, frame):
    """P6.4 real BRAIN operation allowlist is compiled but cannot dispatch."""
    contract = inspect_catalog_read_contract(orchestrator, task_id)
    if not isinstance(frame, dict) or type(frame.get("cmd")) is not str:
        raise OrchestrationError("P6 BRAIN frame invalid")
    allowed = (
        frame["cmd"] in contract["allowed_read_actions"]
        and research_catalog_request_allowed(
            frame["cmd"], {k: v for k, v in frame.items() if k != "cmd"},
            contract["scope"],
            source_alpha_id=orchestrator.load(task_id)["request"].get("alpha_id")))
    return {
        "schema_version": 1, "task_id": task_id,
        "status": ("P6_READ_ONLY_FRAME_SHAPE_VERIFIED_NO_LIVE"
                   if allowed else "P6_BRAIN_FRAME_DENIED"),
        "read_only_frame_shape_valid": allowed,
        "scope": contract["scope"],
        "credential_provider_initialized": False,
        "brain_session_authenticated": False,
        "brain_read_invoked": False,
        "brain_simulation_invoked": False,
        "alpha_submission_invoked": False,
        "permission_to_dispatch": False,
    }


