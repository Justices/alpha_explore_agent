"""P7 offline-only authority deployment contract.

No Docker daemon, live Provider, BRAIN login or Simulation is touched.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_authority_deployment import (
    inspect_local_authority_deployment,
    load_authority_transport_secrets,
    make_protected_authority_mtls_context,
)
from alpha_research.orchestration.research_authority_server import main
from alpha_research.orchestration.research_authority_transport import (
    ResearchAuthorityApplication,
)


class P7AuthorityDeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / "authority.sqlite3"
        self.config = {
            "schema_version": 1,
            "database_file": str(self.db),
            "operator_trust_file": str(self.root / "operator.json"),
            "consumer_private_key_file": str(self.root / "consumer.pem"),
            "ledger_private_key_file": str(self.root / "ledger.pem"),
            "consumer_id": "consumer", "consumer_key_id": "consumer-key",
            "ledger_id": "ledger", "ledger_issuer_id": "issuer",
            "ledger_key_id": "ledger-key",
            "global_max_usd_micros": 9000,
        }
        self.config_file = self.root / "config.json"
        self.config_file.write_text(json.dumps(self.config), encoding="utf-8")
        self.api_file = self.root / "api-token"
        self.admin_file = self.root / "admin-token"
        self.api_file.write_text("api-" + "1" * 48 + "\n", encoding="ascii")
        self.admin_file.write_text("admin-" + "2" * 48 + "\n", encoding="ascii")
        if os.name != "nt":
            self.api_file.chmod(0o600)
            self.admin_file.chmod(0o600)
        self.environ = {
            "ALPHA_RESEARCH_AUTHORITY_API_TOKEN_FILE": str(self.api_file),
            "ALPHA_RESEARCH_AUTHORITY_ADMIN_TOKEN_FILE": str(self.admin_file),
        }

    def _app(self):
        app = ResearchAuthorityApplication(
            self.db, consumer_signer=Ed25519PrivateKey.generate(),
            ledger_signer=Ed25519PrivateKey.generate(),
            operator_trust={"schema_version": 1, "issuers": {},
                            "revoked_grants": []},
            consumer_id="consumer", consumer_key_id="consumer-key",
            ledger_id="ledger", ledger_issuer_id="issuer",
            ledger_key_id="ledger-key", global_max_usd_micros=9000)
        app.initialize_schema()
        return app

    def test_file_secrets_require_owner_separation_and_no_env_fallback(self):
        api, admin = load_authority_transport_secrets(
            self.environ, require_files=True)
        self.assertNotEqual(api, admin)
        self.assertEqual(len(api), 52)
        self.assertEqual(len(admin), 54)
        with self.assertRaises(OrchestrationError):
            load_authority_transport_secrets(
                {**self.environ, "ALPHA_RESEARCH_AUTHORITY_API_TOKEN": api},
                require_files=True)
        with self.assertRaises(OrchestrationError):
            load_authority_transport_secrets(
                {"ALPHA_RESEARCH_AUTHORITY_API_TOKEN": api,
                 "ALPHA_RESEARCH_AUTHORITY_ADMIN_TOKEN": admin},
                require_files=True)
        self.admin_file.write_text(api + "\n", encoding="ascii")
        with self.assertRaises(OrchestrationError):
            load_authority_transport_secrets(
                self.environ, require_files=True)
        self.api_file.write_text(api + "\nnot-a-token", encoding="ascii")
        with self.assertRaises(OrchestrationError):
            load_authority_transport_secrets(
                self.environ, require_files=True)

    @unittest.skipIf(os.name == "nt", "POSIX file ownership and mode test")
    def test_owner_only_secret_and_no_symlink_enforced(self):
        self.api_file.chmod(0o644)
        with self.assertRaises(OrchestrationError):
            load_authority_transport_secrets(
                self.environ, require_files=True)
        self.api_file.chmod(0o600)
        symlink = self.root / "linked-token"
        symlink.symlink_to(self.api_file)
        with self.assertRaises(OrchestrationError):
            load_authority_transport_secrets(
                {**self.environ,
                 "ALPHA_RESEARCH_AUTHORITY_API_TOKEN_FILE": str(symlink)},
                require_files=True)

    def test_readiness_never_bootstraps_missing_state_and_preserves_stop(self):
        # No invocation may create state or change a STOPPED latch.
        pre = inspect_local_authority_deployment(
            self.config, require_file_secrets=True, environ=self.environ)
        self.assertFalse(pre["ready"])
        self.assertFalse(self.db.exists())
        self.assertFalse(pre["permission_to_dispatch"])
        app = self._app()
        ready = inspect_local_authority_deployment(
            self.config, require_file_secrets=True, environ=self.environ)
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["stop_state"], "ARMED")
        self.assertFalse(ready["global_budget_policy_initialized"])
        self.assertFalse(ready["provider_invoice_independently_verified"])
        app.canary.trigger_stop()
        stopped = inspect_local_authority_deployment(
            self.config, require_file_secrets=True, environ=self.environ)
        self.assertTrue(stopped["ready"])  # Admin recovery remains possible
        self.assertEqual(stopped["stop_state"], "STOPPED")
        self.assertEqual(stopped["status"],
                         "LOCAL_RECOVERY_ONLY_STOP_LATCHED")
        self.assertFalse(stopped["permission_to_dispatch"])
        self.assertEqual(app.canary.snapshot()["stop_state"], "STOPPED")

    def test_require_mtls_fails_closed_without_operator_certificates(self):
        with self.assertRaises(OrchestrationError):
            inspect_local_authority_deployment(
                self.config, require_mtls=True,
                require_file_secrets=True, environ=self.environ)
        with self.assertRaises(OrchestrationError):
            make_protected_authority_mtls_context(self.config)
        config = {**self.config,
                  "server_certificate_file": str(self.root / "missing.crt"),
                  "server_private_key_file": str(self.root / "missing.key"),
                  "client_ca_file": str(self.root / "missing-ca.crt")}
        with self.assertRaises(OrchestrationError):
            inspect_local_authority_deployment(
                config, require_mtls=True,
                require_file_secrets=True, environ=self.environ)

    def test_check_ready_cli_has_no_side_effects_or_private_values(self):
        with patch.dict(os.environ, self.environ, clear=False):
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["--config", str(self.config_file),
                             "--port", "8765", "--check-ready",
                             "--require-file-secrets"])
            self.assertEqual(code, 3)
            self.assertFalse(self.db.exists())
            self._app()
            output = io.StringIO()
            with redirect_stdout(output):
                code = main(["--config", str(self.config_file),
                             "--port", "8765", "--check-ready",
                             "--require-file-secrets"])
            self.assertEqual(code, 0)
            report = json.loads(output.getvalue())
            self.assertTrue(report["ready"])
            self.assertFalse(report["permission_to_dispatch"])
            self.assertNotIn(self.environ[
                "ALPHA_RESEARCH_AUTHORITY_API_TOKEN_FILE"], output.getvalue())
            self.assertNotIn("api-" + "1" * 48, output.getvalue())


if __name__ == "__main__":
    unittest.main()
