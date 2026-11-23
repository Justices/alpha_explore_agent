"""Historical import facade for the authority application and HTTP adapter.

New code must import research_authority_application and
research_authority_http directly. This preserves existing external imports
without another authority implementation or service-credential path.
"""
from alpha_research.orchestration.research_authority_application import (
    ResearchAuthorityApplication, _grant_id, _GRANT_ID_CHARS,
)
from alpha_research.orchestration.research_authority_http import (
    make_loopback_authority_server, _bounded_content_length,
    _no_duplicate_json, _forbid_nan, _MAX_BODY,
)

__all__ = ("ResearchAuthorityApplication", "make_loopback_authority_server")
