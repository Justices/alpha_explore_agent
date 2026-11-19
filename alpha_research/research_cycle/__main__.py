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
from alpha_research.research_cycle.durable import DurableOfflineResearchRunner


def main(argv=None):
    p = argparse.ArgumentParser(description="Offline mock-only research cycle")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--state-root", type=Path, default=default_state_root())
    p.add_argument("--fixtures", type=Path, required=True)
    p.add_argument("--research-id", required=True)
    p.add_argument("--objective", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--durable", action="store_true", help="enable persisted multi-round mock workflow")
    p.add_argument("--max-rounds", type=int, default=1)
    p.add_argument("--max-total-experiments", type=int, default=4)
    p.add_argument("--max-per-round", type=int, default=4)
    p.add_argument("--resume-blocked", action="store_true", help="explicitly resume after a safe, non-simulation failure")
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
    model = DecisionRecorder(build_model_client(model_cfg), args.state_root)
    gateway = OfflineSimulationGateway(fixtures)
    if args.durable:
        report = DurableOfflineResearchRunner(
            model, gateway, args.state_root,
            max_rounds=args.max_rounds,
            max_total_experiments=args.max_total_experiments,
            max_per_round=args.max_per_round,
        ).run(research_id=args.research_id, objective=args.objective,
              region=args.region, resume_blocked=args.resume_blocked)
    else:
        if args.resume_blocked:
            p.error("--resume-blocked requires --durable")
        report = OfflineResearchCycle(model, gateway).run_once(
            research_id=args.research_id, objective=args.objective, region=args.region)
        # Hashed path prevents arbitrary research_id from influencing file paths.
        case_key = hashlib.sha256(args.research_id.encode("utf-8")).hexdigest()
        path = args.state_root / "state" / "research_cycle" / (case_key + ".json")
        atomic_write_json(path, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
