"""Local Doctor diagnostics remain secret-safe and network-free."""
import unittest
from unittest.mock import patch

from alpha_research.agents.doctor import check_registered_roles
from alpha_research.agents.registry import (
    bundled_registry_path, load_registry, load_role_profile,
    select_registered_backends,
)


def deepseek_roles():
    return select_registered_backends(
        load_role_profile(),
        load_registry(bundled_registry_path()),
        assignments={
            "research_lead": "deepseek",
            "research_executor": "deepseek",
        },
    )


class DoctorOfflineTests(unittest.TestCase):
    def test_valid_sdk_and_environment_ready_without_provider_call(self):
        with patch("alpha_research.agents.doctor._openai_sdk_present", return_value=True):
            result = check_registered_roles(deepseek_roles(), environ={
                "DEEPSEEK_API_KEY": "private-test-value",
            })
        self.assertEqual(result["status"], "READY_LOCAL")
        self.assertFalse(result["provider_invoked"])
        self.assertFalse(result["brain_invoked"])
        self.assertFalse(result["remote_health_verified"])
        self.assertNotIn("private-test-value", str(result))

    def test_missing_environment_has_specific_diagnostic(self):
        with patch("alpha_research.agents.doctor._openai_sdk_present", return_value=True):
            result = check_registered_roles(deepseek_roles(), environ={})
        self.assertEqual(result["status"], "BLOCKED_LOCAL")
        for role in ("research_lead", "research_executor"):
            self.assertIn("DEEPSEEK_API_KEY", result["roles"][role]["reason"])
            self.assertFalse(result["roles"][role]["ready_local"])

    def test_missing_sdk_is_distinguished_from_missing_key(self):
        with patch("alpha_research.agents.doctor._openai_sdk_present", return_value=False):
            result = check_registered_roles(deepseek_roles(), environ={
                "DEEPSEEK_API_KEY": "private-test-value",
            })
        self.assertEqual(result["status"], "BLOCKED_LOCAL")
        for row in result["roles"].values():
            self.assertIn("OpenAI Python SDK not installed", row["reason"])
            self.assertNotIn("environment missing", row["reason"])
            self.assertNotIn("private-test-value", row["reason"])


if __name__ == "__main__":
    unittest.main()
