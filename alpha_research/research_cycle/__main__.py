"""Manual, mock-only smoke entrypoint.

Usage:
    python -m alpha_research.research_cycle --config model.json \
      --fixtures simulation.json --research-id R1 --objective "demo" --region CHN
"""
import argparse
import json
from pathlib import Path
from alpha_research.integrations.model import build_model_client, DecisionRecorder
from alpha_research.state.paths import default_state_root
from alpha_research.state.store import atomic_write_json
import hashlib
from alpha_research.research_cycle.engine import OfflineResearchCycle, OfflineSimulationGateway


def main(argv=None):
    p = argparse.ArgumentParser(description="Offline mock-only research cycle")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--state-root", type=Path, default=default_state_root())
    p.add_argument("--fixtures", type=Path, required=True)
    p.add_argument("--research-id", required=True)
    p.add_argument("--objective", required=True)
    p.add_argument("--region", required=True)
    args = p.parse_args(argv)
    config = json.loads(args.config.read_text(encoding="utf-8"))
    model_cfg = config.get("model", config)
    if model_cfg.get("provider") != "role_router":
        p.error("offline cycle requires role_router model configuration")
    backends = model_cfg.get("backends", {})
    if not isinstance(backends, dict) or not backends or any(
            not isinstance(v, dict) or v.get("provider") != "mock"
            for v in backends.values()):
        p.error("offline cycle permits only mock model providers")
    fixtures = json.loads(args.fixtures.read_text(encoding="utf-8"))
    report = OfflineResearchCycle(
        DecisionRecorder(build_model_client(model_cfg), args.state_root), OfflineSimulationGateway(fixtures)
    ).run_once(research_id=args.research_id, objective=args.objective, region=args.region)
    # Hashed path prevents arbitrary research_id from influencing file paths.
    case_key = hashlib.sha256(args.research_id.encode("utf-8")).hexdigest()
    path = args.state_root / "state" / "research_cycle" / (case_key + ".json")
    atomic_write_json(path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
