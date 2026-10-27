"""Standalone command entry point for one research batch or recovery."""
import argparse
import json
import os
import sys
from pathlib import Path

from .agent import ResearchAgent
from .gateway import Gateway
from .model_client import ConfiguredModelClient


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nastu_quant")
    parser.add_argument("--state-root", type=Path,
                        default=Path(os.environ.get("NASTU_QUANT_HOME",
                                                    Path.home() / "workspace" / "nastu_quant")))
    parser.add_argument("--config", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run-once", help="execute one admitted research batch")
    run.add_argument("--objective", required=True)
    run.add_argument("--dataset")
    series = sub.add_parser("run", help="execute a bounded serial research case")
    series.add_argument("--objective", required=True)
    series.add_argument("--dataset")
    series.add_argument("--max-batches", type=int, required=True)
    recovery = sub.add_parser("recover", help="reconcile an unfinished batch")
    recovery.add_argument("batch_dir", type=Path)
    void = sub.add_parser("void", help="archive an unsubmitted draft")
    void.add_argument("batch_dir", type=Path)
    void.add_argument("--reason", required=True)
    sub.add_parser("status", help="inspect local state without platform access")
    sub.add_parser("stop", help="block new simulations")
    sub.add_parser("resume", help="clear the stop flag by explicit command")
    args = parser.parse_args(argv)
    root = args.state_root.resolve()
    stop_flag = root / "state" / "stop.flag"
    if args.command == "stop":
        stop_flag.parent.mkdir(parents=True, exist_ok=True)
        stop_flag.write_text("stop\n", encoding="utf-8")
        print(json.dumps({"stopped": True}))
        return 0
    if args.command == "resume":
        stop_flag.unlink(missing_ok=True)
        print(json.dumps({"stopped": False}))
        return 0
    if args.command == "status":
        runs = root / "runs"
        unfinished = []
        if runs.exists():
            for context in runs.rglob("batch_context.json"):
                if not (context.parent / "results.json").exists() and not (
                        context.parent / "void.json").exists():
                    unfinished.append(str(context.parent))
        print(json.dumps({"stopped": stop_flag.exists(),
                          "unfinished_batches": unfinished}, ensure_ascii=False))
        return 0
    if args.command == "void":
        try:
            agent = ResearchAgent(None, None, {}, root / "runs")
            record = agent.void_batch(args.batch_dir, args.reason)
            print(json.dumps(record, ensure_ascii=False))
            return 0
        except Exception as exc:
            print(f"void rejected: {exc}", file=sys.stderr)
            return 1
    if stop_flag.exists():
        print("research is stopped; no new platform work will start", file=sys.stderr)
        return 2
    config_path = args.config or root / "config" / "nastu_quant.json"
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        model = ConfiguredModelClient(cfg["model"])
    except (OSError, ValueError, KeyError) as exc:
        print(f"configuration unavailable: {exc}", file=sys.stderr)
        return 2
    os.environ["NASTU_QUANT_STATE_DIR"] = str(root / "state")
    gateway = Gateway()
    agent = ResearchAgent(gateway, model, cfg, root / "runs",
                          stop_flag=stop_flag)
    try:
        gateway.start()
        if args.command == "run-once":
            result = agent.run_once(args.objective, dataset_id=args.dataset)
        elif args.command == "run":
            result = agent.run_case(args.objective, args.max_batches,
                                    dataset_id=args.dataset)
        else:
            result = agent.recover(args.batch_dir)
        if args.command == "run":
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(json.dumps({"dataset_id": result["dataset_id"],
                              "alpha_ids": result["alpha_ids"],
                              "candidate_status": result["candidate_status"]},
                             ensure_ascii=False))
        return 0
    except Exception as exc:
        print(f"research paused: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            gateway.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
