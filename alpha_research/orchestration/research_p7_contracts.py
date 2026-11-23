"""Legacy import surface; authoritative vocabulary lives in release_contracts.

No production entitlement is represented by this module.
"""
from alpha_research.orchestration.release_contracts import (
    PROVIDER_HOSTNAMES, BILLING_STATUSES, BRAIN_STATUSES, CANARY_STATUSES,
    brain_read_status, canary_status,
)

__all__ = (
    "PROVIDER_HOSTNAMES", "BILLING_STATUSES", "BRAIN_STATUSES",
    "CANARY_STATUSES", "brain_read_status", "canary_status",
)
