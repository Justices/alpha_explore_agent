"""Finish a one-POST pilot from its saved BRAIN readback and model response."""
import hashlib
from pathlib import Path

from alpha_research.integrations.model import DecisionRecorder, build_model_client
from alpha_research.integrations.model_evidence import check_trusted_path
from alpha_research.orchestration.engine import MainOrchestrator, pool_snapshot
from alpha_research.orchestration.recovery import RecoveryCoordinator, RecoveryError
from alpha_research.orchestration.workflow_contract import project_existing_qualification
from alpha_research.state.fingerprint import fingerprint_json
from alpha_research.state.json_contract import read_strict_object
from alpha_research.state.store import verify_prereg


class SavedPilotEvidence:
    """Only local reads and verified response replay are exposed to recovery."""

    def __init__(self, root, task, config, call_id):
        self.runs_root = root / "runs"
        self.request = task["request"]
        self.research_id = (task.get("research_identity") or {}).get("research_id")
        self.call_id = call_id
        self.recorder = DecisionRecorder(
            build_model_client(config["model"]), root / "state",
            governance=config["model_governance"])

    def recover(self, batch_dir):
        batch = check_trusted_path(Path(batch_dir), label="pilot batch directory")
        if not batch.is_relative_to(self.runs_root):
            raise RecoveryError("Pilot batch is outside the saved runs directory")

        def read(name):
            path = check_trusted_path(batch / name, label="pilot batch evidence")
            return read_strict_object(path, name, error_type=RecoveryError)

        context = read("batch_context.json")
        prereg = read("prereg.json")
        state = read("state.json")
        intent = read("simulation_intent.json")
        result = read("results.json")
        raw = read("simulation_raw.json")
        prereg_digest = hashlib.sha256((batch / "prereg.json").read_bytes()).hexdigest()
        alphas = result.get("alpha_ids")
        expressions = [row["expression"] for row in prereg["variants"]]
        settings = intent.get("settings")
        if (not isinstance(self.research_id, str)
                or self.research_id != context.get("research_id")
                or not verify_prereg(batch / "prereg.json", state)
                or intent.get("prereg_sha256") != prereg_digest
                or intent.get("status") != "READ_BACK"
                or settings != prereg.get("simulation_settings")
                or not isinstance(settings, dict)
                or any(settings.get(key) != self.request[key]
                       for key in ("region", "delay", "universe"))
                or self.request["dataset_ids"] != [prereg.get("dataset_id")]
                or self.request["objective"] != prereg.get("objective")
                or not isinstance(alphas, list) or not 1 <= len(alphas) <= 8
                or len(alphas) != len(expressions)
                or any(not isinstance(alpha, str) or not alpha for alpha in alphas)
                or len(set(alphas)) != len(alphas)
                or intent.get("alpha_ids") != alphas
                or result.get("child_batches") is not None
                or result.get("attribution_status") != "complete"):
            raise RecoveryError("Pilot requires a verified, attributed READ_BACK batch")
        rows = raw.get("alpha_results")
        if (not isinstance(rows, list) or len(rows) != len(alphas)
                or any(not isinstance(row, dict) or row.get("success") is not True
                       or row.get("expression") != expression or row.get("alpha_id") != alpha
                       for row, expression, alpha in zip(rows, expressions, alphas))):
            raise RecoveryError("Pilot raw simulation receipt differs from preregistration")
        return result

    def attribute_saved_batch(self, batch, result):
        raise RecoveryError("saved Pilot batch attribution is missing")

    def _decide(self, task, context, *, batch_dir):
        return self.recorder.replay_saved_decision(
            self.research_id, self.call_id, task=task, context=context, batch_dir=batch_dir)


class SavedPilotProjection:
    def __init__(self, root, config):
        self.root = root
        self.config = config

    def run(self, workflow, step, request, checkpoints, context=None):
        if workflow != "regular":
            raise RecoveryError("saved Pilot projection supports Regular only")
        if step == "qualify":
            return project_existing_qualification(workflow, checkpoints)
        if step == "collect":
            return {
                "mode": "supervised_local_real_pilot",
                "production_release_authorized": False,
                "qualification": checkpoints["qualify"],
                "dataset_reviews": checkpoints["execute"]["dataset_reviews"],
                "pool": pool_snapshot(self.root, self.config),
            }
        raise RecoveryError("saved Pilot projection requires a verified execute checkpoint")


def recover_saved_pilot(root, task_id, call_id, config):
    runner = MainOrchestrator(root, adapter=SavedPilotProjection(root, config), config=config)
    task = runner.load(task_id)
    if (task["workflow"] != "regular"
            or (task["checkpoints"].get("validate") or {}).get("mode")
            != "supervised_local_real_pilot"):
        raise RecoveryError("saved-review recovery supports supervised Local Regular Pilot only")
    if task["status"] == "COMPLETED":
        return task
    identity_path = runner.root.parent / "execution_identities" / f"{task_id}.json"
    document = read_strict_object(identity_path, "saved Pilot identity", error_type=RecoveryError)
    identity = document.get("identity")
    if (not isinstance(identity, dict)
            or document.get("identity_sha256") != fingerprint_json(identity)
            or identity.get("task_id") != task_id or identity.get("workflow") != "regular"
            or identity.get("request_sha256") != task["request_sha256"]
            or identity.get("model_profile_sha256") != fingerprint_json(config["model"])
            or identity.get("model_governance_policy_sha256")
            != fingerprint_json(config["model_governance"])):
        raise RecoveryError("saved Pilot execution identity differs from current configuration")
    if task["steps"]["execute"] != "DONE":
        evidence = SavedPilotEvidence(root, task, config, call_id)
        RecoveryCoordinator(root).recover_task(
            runner, task_id, gateway=None, agent=evidence,
            expected_workflow="regular", saved_pilot=True)
    elif not task.get("reconciliation"):
        raise RecoveryError("saved Pilot projection is missing a coordinator receipt")
    return runner.resume(task_id)
