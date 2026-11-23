"""Compatibility entrypoints for the canonical research readiness modules.

Provider, Catalog, release matrix and offline rehearsal now have separate
owners. P6 status values are retained for audit record interoperability.
"""
from alpha_research.orchestration.research_provider_audit import (
    _PROVIDER_ENDPOINTS, inspect_p6_provider_plan,
    inspect_p6_provider_metering, inspect_p6_signed_provider_readback,
)
from alpha_research.orchestration.research_brain_read_audit import (
    inspect_p6_brain_catalog_frame,
)
from alpha_research.orchestration.research_readiness_matrix import (
    _METERING_VERDICTS, inspect_p6_integrated_release,
)
from alpha_research.orchestration.research_canary_rehearsal import (
    run_p6_offline_canary,
)

__all__ = (
    "inspect_p6_provider_plan", "inspect_p6_provider_metering",
    "inspect_p6_signed_provider_readback", "inspect_p6_brain_catalog_frame",
    "inspect_p6_integrated_release", "run_p6_offline_canary",
)
