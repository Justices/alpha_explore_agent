"""Evidence summary for simulated candidates; never grants Candidate status."""

from datetime import datetime, timezone


METRICS = ("sharpe", "fitness", "turnover", "returns", "drawdown", "margin")


def _view(details):
    block = details.get("is") if isinstance(details, dict) else None
    if not isinstance(block, dict):
        return {"metrics": {}, "checks": None, "pending": ["is_block_unavailable"]}
    metrics = {key: block[key] for key in METRICS
               if isinstance(block.get(key), (int, float))
               and not isinstance(block[key], bool)}
    checks = block.get("checks")
    pending = [] if isinstance(checks, list) and checks else ["platform_checks_unavailable"]
    return {"metrics": metrics, "checks": checks if isinstance(checks, list) else None,
            "pending": pending}


def record_pending_audit(candidate: dict, *, workflow: str,
                         source: dict | None = None):
    """Preserve comparable live results while requiring the full family audit."""
    candidate_view = _view(candidate)
    source_view = _view(source) if source is not None else None
    deltas = {}
    if source_view is not None:
        for key, value in candidate_view["metrics"].items():
            if key in source_view["metrics"]:
                deltas[key] = value - source_view["metrics"][key]
    return {
        "schema_version": 1, "workflow": workflow,
        "candidate_alpha_id": candidate.get("id"),
        "source_alpha_id": source.get("id") if source is not None else None,
        "candidate": candidate_view, "source": source_view,
        "metric_deltas": deltas,
        "status": "pending_full_candidate_audit",
        "pending": ["complete_applicable_family_audit", *candidate_view["pending"]],
        "observed_at": datetime.now(timezone.utc).isoformat(),
    }
