"""Gate logic (FR-10, AGENT_DESIGN.md §3.3): alpha lifecycle states and
the structurally ordered RA Gate -> correlation -> Candidate verdict.

The order is enforced by the state machine: correlation may only be
checked from state RA_PASSED. The Gateway calls
`assert_correlation_allowed` before forwarding any check_correlation
command — a second, independent layer (acceptance A2).
"""

ALPHA_STATES = {
    "CREATED", "SIMULATED", "CLASSIFIED", "BELOW_THRESHOLD",
    "RA_PASSED", "RA_FAILED", "CORR_PASSED", "CORR_REJECTED",
    "CANDIDATE", "TECH_ERROR",
}

# Legal transitions of the alpha lifecycle (design §3.3).
TRANSITIONS = {
    "CREATED": {"SIMULATED"},
    "SIMULATED": {"CLASSIFIED", "TECH_ERROR"},
    "CLASSIFIED": {"BELOW_THRESHOLD", "RA_PASSED", "RA_FAILED"},
    "RA_PASSED": {"CORR_PASSED", "CORR_REJECTED"},
    "CORR_PASSED": {"CANDIDATE"},
    "BELOW_THRESHOLD": set(),
    "RA_FAILED": set(),
    "CORR_REJECTED": set(),
    "CANDIDATE": set(),
    "TECH_ERROR": set(),
}


class GateOrderError(RuntimeError):
    """Raised when a gate step is attempted out of order."""


def can_transition(src: str, dst: str) -> bool:
    return dst in TRANSITIONS.get(src, set())


def transition(alpha: dict, dst: str) -> dict:
    src = alpha["state"]
    if not can_transition(src, dst):
        raise GateOrderError(f"illegal alpha transition {src} -> {dst}")
    alpha["state"] = dst
    return alpha


def assert_correlation_allowed(alpha: dict) -> None:
    """Precondition for any correlation check: RA must have passed."""
    if alpha.get("state") != "RA_PASSED":
        raise GateOrderError(
            f"correlation check forbidden in state {alpha.get('state')}: "
            "RA Gate must pass first (FR-10)")


def sharpe_gate(sharpe: float, cfg: dict) -> bool:
    return sharpe is not None and sharpe >= cfg["gate"]["sharpe_min"]


def ra_gate(ra_fail_count: int) -> bool:
    """RA Gate passes iff the fail count is exactly zero."""
    return ra_fail_count == 0


def correlation_verdict(prod_corr, self_corr, pairwise_corrs, cfg: dict):
    """Candidate correlation verdict with STRICT less-than thresholds
    (FR-10 as tightened 2026-10-23: prod/self < 0.70, pairwise < 0.40).

    Returns (ok, reasons). A value exactly equal to a threshold FAILS.
    """
    g = cfg["gate"]
    reasons = []
    if prod_corr is None or not prod_corr < g["prod_corr_max"]:
        reasons.append(f"prod_corr {prod_corr} not < {g['prod_corr_max']}")
    if self_corr is None or not self_corr < g["self_corr_max"]:
        reasons.append(f"self_corr {self_corr} not < {g['self_corr_max']}")
    for other_id, value in (pairwise_corrs or {}).items():
        if value is None or not value < g["pairwise_corr_max"]:
            reasons.append(
                f"pairwise vs {other_id}: {value} not < {g['pairwise_corr_max']}")
    return (not reasons, reasons)
