"""Authority domain and loopback transport must stay separately testable."""
import json
import unittest

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.research_authority_application import (
    ResearchAuthorityApplication,
)
from alpha_research.orchestration.research_authority_http import (
    make_loopback_authority_server, _bounded_content_length,
    _no_duplicate_json, _forbid_nan,
)
from alpha_research.orchestration.research_authority_transport import (
    ResearchAuthorityApplication as LegacyAuthority,
    make_loopback_authority_server as legacy_server,
)


class AuthorityBoundaryTests(unittest.TestCase):
    def test_old_imports_still_refer_to_canonical_types(self):
        self.assertIs(ResearchAuthorityApplication, LegacyAuthority)
        self.assertIs(make_loopback_authority_server, legacy_server)

    def test_reject_untrusted_application_before_socket_creation(self):
        with self.assertRaises(OrchestrationError):
            make_loopback_authority_server(
                object(), api_token="a" * 32, admin_token="b" * 32)

    def test_bounded_http_body_cannot_overflow_integer_parsing(self):
        for invalid in (None, "", "0", "-1", "0x10", " 1", "65537",
                        "9" * 10000, True, "１２", "1.0"):
            with self.subTest(raw_type=type(invalid).__name__):
                self.assertIsNone(_bounded_content_length(invalid))
        for accepted, expected in (("1", 1), ("00001", 1),
                                   ("65536", 65536)):
            self.assertEqual(_bounded_content_length(accepted), expected)

    def test_json_rejects_duplicate_fields_and_nonfinite_values(self):
        with self.assertRaises(ValueError):
            json.loads('{"a":1,"a":2}', object_pairs_hook=_no_duplicate_json)
        with self.assertRaises(ValueError):
            json.loads('{"bad":NaN}', parse_constant=_forbid_nan)


if __name__ == "__main__":
    unittest.main()
