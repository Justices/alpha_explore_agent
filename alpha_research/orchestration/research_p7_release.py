"""Compatibility entrypoint for the canonical negative research release audit.

Do not define new policy here. The release_matrix handles aggregation and
release_evidence handles strict validation. No transport is enabled.
"""
from alpha_research.orchestration.release_matrix import (
    inspect_research_release,
)
from alpha_research.orchestration.release_evidence import (
    _check_negative, _check_billing, _check_brain, _check_canary,
)

inspect_p7_integrated_release = inspect_research_release
