"""Built-in synthetic, fixture-only execution for a previously prepared Task.

No domain Dispatch, model, Gateway, ModelCallLedger, Simulation or candidate
records are invoked. It exercises the real persisted orchestration state
machine and qualification projection with clearly synthetic checkpoints.
"""
from alpha_research.orchestration.offline_adapter import OfflineResearchAdapter
from alpha_research.orchestration.workflow_contract import (
    validate_workflow_request, project_existing_qualification,
)


def build_prepared_mock_adapter(workflow, request):
    validate_workflow_request(workflow, request)
    if workflow == "regular":
        execution = {
            "status": "SYNTHETIC_ONLY",
            "completed": [{"alpha_ids": ["SYNTHETIC_REGULAR"],
                           "candidate_status": {
                               "SYNTHETIC_REGULAR": "EVIDENCE_PENDING"}}],
        }
    else:
        execution = {
            "status": "SYNTHETIC_ONLY",
            "candidate_id": "SYNTHETIC_" + workflow.upper(),
            "candidate_status": "EVIDENCE_PENDING",
            "preferred_status": "PENDING_PREFERRED",
            "candidate_ref": "synthetic://no-candidate-record",
            "preferred_ref": "synthetic://no-preferred-record",
        }
    qualification = project_existing_qualification(
        workflow, {"execute": execution})
    return OfflineResearchAdapter({
        workflow + ".validate": {
            "validated": True, "mode": "synthetic_fixture",
            "external_calls": False},
        workflow + ".execute": execution,
        workflow + ".qualify": qualification,
        workflow + ".collect": {
            "status": "SYNTHETIC_ONLY", "mode": "synthetic_fixture",
            "domain_dispatch_invoked": False, "brain_invoked": False,
            "model_invoked": False, "candidate_record_created": False,
        },
    })
