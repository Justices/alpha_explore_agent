"""Offline identity snapshot tests: no provider or BRAIN sessions."""
import copy
import hashlib
import hmac
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from alpha_research.agents.registry import (
    bundled_registry_path, load_registry, load_role_profile, select_registered_backends,
)
from alpha_research.orchestration.engine import MainOrchestrator, OrchestrationError
from alpha_research.orchestration.execution_proposal import persist_execution_proposal
from alpha_research.orchestration.approval_review import KEY_ENV, SIGNING_CONTEXT
from alpha_research.state.fingerprint import canonical_json_bytes
from alpha_research.orchestration.execution_identity import (
    build_execution_identity, persist_execution_identity, inspect_execution_identity,
    verify_identity_bound_review,
)

POLICY = {"max_calls": 3, "max_reported_tokens": 2000, "failover": "manual"}


class ExecutionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runner = MainOrchestrator(self.temp.name)
        self.task = self.runner.create(
            "regular", {"objective": "offline identity", "region": "CHN"})
        persist_execution_proposal(self.runner, self.task["task_id"])
        registry = load_registry(bundled_registry_path())
        self.model_config = select_registered_backends(
            load_role_profile(), registry, assignments={
                "research_lead": "deepseek", "research_executor": "deepseek",
            })

    def test_snapshot_reuses_existing_budget_identity_without_creating_ledger(self):
        result = persist_execution_identity(
            self.runner, self.task["task_id"], self.model_config, POLICY)
        snapshot = result["identity"]
        self.assertEqual(snapshot["status"], "PREPARED_NOT_AUTHORIZED")
        self.assertEqual(snapshot["task_id"], self.task["task_id"])
        self.assertEqual(set(snapshot["role_bindings"]),
                         {"research_lead", "research_executor"})
        self.assertEqual(snapshot["gateway"]["status"], "NOT_STARTED")
        self.assertIsNone(snapshot["gateway"]["session_identity"])
        self.assertEqual(snapshot["model_budget"]["status"], "UNBOUND_DOMAIN_RESEARCH_ID")
        self.assertIsNone(snapshot["model_budget"]["research_id"])
        self.assertEqual(snapshot["orchestrator_budget"]["reserved"], 0)
        self.assertEqual(snapshot["execution_lock"], "state/agent.lock")
        for field in ("allow_model_call", "allow_brain_simulation",
                      "allow_alpha_submission"):
            self.assertFalse(snapshot[field])
        self.assertEqual(
            result, inspect_execution_identity(
                self.runner, self.task["task_id"], self.model_config, POLICY))
        self.assertEqual(
            result, persist_execution_identity(
                self.runner, self.task["task_id"], self.model_config, POLICY))
        self.assertEqual(self.runner.load(self.task["task_id"]), self.task)

    def test_selected_role_or_budget_drift_blocks_reuse(self):
        persist_execution_identity(
            self.runner, self.task["task_id"], self.model_config, POLICY)
        changed_policy = dict(POLICY, max_calls=4)
        with self.assertRaisesRegex(OrchestrationError, "changed"):
            persist_execution_identity(
                self.runner, self.task["task_id"], self.model_config, changed_policy)
        with self.assertRaisesRegex(OrchestrationError, "drift"):
            inspect_execution_identity(
                self.runner, self.task["task_id"], self.model_config, changed_policy)

        changed_models = copy.deepcopy(self.model_config)
        changed_models["task_roles"]["construct_batch"] = "research_executor"
        with self.assertRaisesRegex(OrchestrationError, "drift"):
            inspect_execution_identity(
                self.runner, self.task["task_id"], changed_models, POLICY)

    def test_reject_mock_inline_credential_and_missing_proposal(self):
        bad = copy.deepcopy(self.model_config)
        bad["backends"]["registry:deepseek"]["api_key"] = "not-a-real-key"
        with self.assertRaises(OrchestrationError):
            build_execution_identity(
                self.runner, self.task["task_id"], bad, POLICY)

        with tempfile.TemporaryDirectory() as td:
            other = MainOrchestrator(td)
            task = other.create(
                "regular", {"objective": "unplanned", "region": "CHN"})
            with self.assertRaises(OrchestrationError):
                build_execution_identity(
                    other, task["task_id"], self.model_config, POLICY)

    def test_review_receipt_must_match_persisted_selected_model_profile(self):
        identity = persist_execution_identity(
            self.runner, self.task["task_id"], self.model_config, POLICY)["identity"]
        now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
        key = "test-only-pinned-review-key-at-least-32-characters"
        claims = {
            "schema_version": 1, "approval_id": "f" * 32,
            "issuer": "offline-contract-test", "task_id": self.task["task_id"],
            "workflow": self.task["workflow"],
            "request_sha256": self.task["request_sha256"],
            "proposal_sha256": identity["proposal_sha256"],
            "model_profile_sha256": identity["model_profile_sha256"],
            "operation": "review_execution_proposal",
            "max_execution_attempts": 1,
            "issued_at": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
        }
        signature = hmac.new(
            key.encode("utf-8"), SIGNING_CONTEXT + canonical_json_bytes(claims),
            hashlib.sha256).hexdigest()
        receipt = {"claims": claims, "signature": signature}
        args = {"environ": {KEY_ENV: key}, "now": now}
        checked = verify_identity_bound_review(
            self.runner, self.task["task_id"],
            self.model_config, POLICY, receipt, **args)
        self.assertTrue(checked["verified"])
        self.assertFalse(checked["model_call_authorized"])
        consumed = verify_identity_bound_review(
            self.runner, self.task["task_id"], self.model_config, POLICY,
            receipt, consume=True, **args)
        self.assertFalse(consumed["simulation_authorized"])
        with self.assertRaisesRegex(OrchestrationError, "already consumed"):
            verify_identity_bound_review(
                self.runner, self.task["task_id"], self.model_config, POLICY,
                receipt, consume=True, **args)
        different = copy.deepcopy(self.model_config)
        different["task_roles"]["attribute_results"] = "research_lead"
        with self.assertRaisesRegex(OrchestrationError, "drift"):
            verify_identity_bound_review(
                self.runner, self.task["task_id"], different, POLICY,
                receipt, **args)

    def test_preparation_never_unlocks_production_execution(self):
        from alpha_research.orchestration.production_adapter import ProductionWorkflowAdapter

        persist_execution_identity(
            self.runner, self.task["task_id"], self.model_config, POLICY)
        live = MainOrchestrator(
            self.temp.name,
            adapter=ProductionWorkflowAdapter(self.temp.name, {}))
        result = live.execute(self.task["task_id"])
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["steps"]["execute"], "PENDING")
        self.assertEqual(result["budget"]["used"], 0)
        self.assertEqual(result["budget"]["reserved"], 0)


if __name__ == "__main__":
    unittest.main()
