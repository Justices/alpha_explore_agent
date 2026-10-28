"""Standalone command entry point for one research batch or recovery."""
import argparse
import json
import os
import re
import sys
from .agent import ResearchAgent
from .gateway import Gateway
from .model_client import ConfiguredModelClient
from .campaign import SingleWriterLock, WriterLockError
from .knowledge import KnowledgeService
from .paths import default_state_root, expand_path


def _path_argument(value):
    return expand_path(value)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="nastu_quant")
    parser.add_argument("--state-root", type=_path_argument, default=default_state_root())
    parser.add_argument("--config", type=_path_argument)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run-once", help="execute one admitted research batch")
    run.add_argument("--objective", required=True)
    run.add_argument("--dataset")
    run.add_argument("--diagnosis-id")
    run.add_argument("--remedy-id")
    run.add_argument("--allow-remedy-round-overrun", action="store_true")
    series = sub.add_parser("run", help="execute a bounded serial research case")
    series.add_argument("--objective", required=True)
    series.add_argument("--dataset")
    series.add_argument("--max-batches", type=int, required=True)
    series.add_argument("--diagnosis-id")
    series.add_argument("--remedy-id")
    series.add_argument("--allow-remedy-round-overrun", action="store_true")
    recovery = sub.add_parser("recover", help="reconcile an unfinished batch")
    recovery.add_argument("batch_dir", type=_path_argument)
    void = sub.add_parser("void", help="archive an unsubmitted draft")
    void.add_argument("batch_dir", type=_path_argument)
    void.add_argument("--reason", required=True)
    validity = sub.add_parser("evidence-validity",
                              help="mark an indexed evidence item with an explicit validity")
    validity.add_argument("evidence_id")
    validity.add_argument("--status", choices=("valid", "void", "falsified", "pending"),
                          required=True)
    validity.add_argument("--reason", required=True)
    validity.add_argument("--operator", default="user")
    knowledge = sub.add_parser("knowledge", help="inspect and maintain local research knowledge")
    knowledge_sub = knowledge.add_subparsers(dest="knowledge_command", required=True)
    listing = knowledge_sub.add_parser("list", help="list managed knowledge items")
    listing.add_argument("--kind", choices=("all", "operator", "field", "remedy", "evidence"),
                         default="all")
    show = knowledge_sub.add_parser("show", help="show one managed knowledge item")
    show.add_argument("--kind", choices=("operator", "field", "remedy", "evidence"),
                      required=True)
    show.add_argument("--id", required=True)
    note = knowledge_sub.add_parser("note", help="manage exact operator and field notes")
    note_sub = note.add_subparsers(dest="note_command", required=True)
    note_put = note_sub.add_parser("put", help="add or explicitly replace an exact note")
    note_put.add_argument("--kind", choices=("operator", "field"), required=True)
    note_put.add_argument("--key", required=True)
    note_put.add_argument("--content-file", type=_path_argument, required=True)
    note_put.add_argument("--source", required=True)
    note_put.add_argument("--date", default="unknown")
    note_put.add_argument("--validity", choices=("valid", "void", "falsified", "pending"),
                          default="valid")
    note_put.add_argument("--replace", action="store_true")
    note_put.add_argument("--reason")
    note_put.add_argument("--operator", default="user")
    remedy = knowledge_sub.add_parser("remedy", help="register and review remedies")
    remedy_sub = remedy.add_subparsers(dest="remedy_command", required=True)
    remedy_add = remedy_sub.add_parser("add", help="register a remedy as pending review")
    remedy_add.add_argument("--file", type=_path_argument, required=True)
    remedy_add.add_argument("--operator", default="user")
    remedy_review = remedy_sub.add_parser("review", help="review a registered remedy")
    remedy_review.add_argument("--id", required=True)
    remedy_review.add_argument("--status",
                               choices=("pending_review", "verified", "falsified", "void"),
                               required=True)
    remedy_review.add_argument("--reviewer", required=True)
    remedy_review.add_argument("--reason", required=True)
    search = knowledge_sub.add_parser("search", help="search local indexed evidence")
    search.add_argument("--dataset", default="*")
    search.add_argument("--mechanism", action="append", default=[])
    search.add_argument("--failure-signature", action="append", default=[])
    search.add_argument("--field-generation", action="append", default=[])
    search.add_argument("--region")
    search.add_argument("--delay", type=int)
    search.add_argument("--universe")
    search.add_argument("--text", default="")
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--include-history", action="store_true",
                         help="return historical-only evidence; never mixed with valid results")
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
                batch_dir = context.parent
                if (batch_dir / "void.json").exists():
                    continue
                intent_path = batch_dir / "simulation_intent.json"
                results_path = batch_dir / "results.json"
                try:
                    closed = (results_path.exists() and intent_path.exists() and
                              json.loads(intent_path.read_text(encoding="utf-8")).get(
                                  "status") == "READ_BACK")
                except (OSError, ValueError):
                    closed = False
                if not closed:
                    unfinished.append(str(batch_dir))
                    continue
                prereg_path = batch_dir / "prereg.json"
                if prereg_path.exists():
                    try:
                        prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
                        if prereg.get("remedy_reference"):
                            diagnosis = json.loads((batch_dir / "diagnosis.json").read_text(
                                encoding="utf-8"))
                            if (diagnosis.get("remedy_outcome") or {}).get("status") != "recorded":
                                unfinished.append(str(batch_dir))
                    except (OSError, ValueError):
                        unfinished.append(str(batch_dir))
            for batch_dir in runs.glob("*/*/*/*/*"):
                if (batch_dir.is_dir() and
                        re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{8}", batch_dir.name) and
                        not (batch_dir / "batch_context.json").exists() and
                        not (batch_dir / "void.json").exists() and
                        str(batch_dir) not in unfinished):
                    unfinished.append(str(batch_dir))
        campaign_root = root / "state" / "campaigns"
        campaigns = []
        if campaign_root.exists():
            for path in sorted(campaign_root.glob("*.json")):
                try:
                    campaigns.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    campaigns.append({"path": str(path), "status": "unreadable"})
        print(json.dumps({"stopped": stop_flag.exists(),
                          "unfinished_batches": unfinished,
                          "campaigns": campaigns}, ensure_ascii=False))
        return 0
    if args.command == "void":
        try:
            with SingleWriterLock(root / "state" / "agent.lock"):
                agent = ResearchAgent(None, None, {}, root / "runs")
                record = agent.void_batch(args.batch_dir, args.reason)
            print(json.dumps(record, ensure_ascii=False))
            return 0
        except Exception as exc:
            print(f"void rejected: {exc}", file=sys.stderr)
            return 1
    if args.command == "evidence-validity":
        try:
            cfg_path = args.config or root / "config" / "nastu_quant.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
            with SingleWriterLock(root / "state" / "agent.lock"):
                service = KnowledgeService(root / "state", root / "runs", cfg)
                result = service.set_evidence_validity(
                    args.evidence_id, args.status, args.reason, args.operator)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except Exception as exc:
            print(f"evidence update rejected: {exc}", file=sys.stderr)
            return 1
    if args.command == "knowledge":
        try:
            cfg_path = args.config or root / "config" / "nastu_quant.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
            with SingleWriterLock(root / "state" / "agent.lock"):
                service = KnowledgeService(root / "state", root / "runs", cfg)
                if args.knowledge_command == "list":
                    result = service.list_managed(args.kind)
                elif args.knowledge_command == "show":
                    result = service.get_managed(args.kind, args.id)
                elif args.knowledge_command == "note":
                    content = args.content_file.read_text(encoding="utf-8-sig")
                    result = service.put_note(
                        args.kind, args.key, content, args.source, date=args.date,
                        validity=args.validity, replace=args.replace, reason=args.reason,
                        operator=args.operator)
                elif args.knowledge_command == "remedy":
                    if args.remedy_command == "add":
                        payload = json.loads(args.file.read_text(encoding="utf-8-sig"))
                        result = service.add_remedy(payload, operator=args.operator)
                    else:
                        result = service.review_remedy(
                            args.id, args.status, reviewer=args.reviewer,
                            reason=args.reason)
                else:
                    tags = {"dataset_id": args.dataset,
                            "mechanism": args.mechanism,
                            "failure_signature": args.failure_signature,
                            "field_generation": args.field_generation,
                            "scope": {"region": args.region, "delay": args.delay,
                                      "universe": args.universe}}
                    result = service.search_evidence(
                        tags, k=args.limit, query_text=args.text,
                        include_history=args.include_history)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except Exception as exc:
            print(f"knowledge operation rejected: {exc}", file=sys.stderr)
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
    knowledge_cfg = cfg.get("knowledge") or {}
    configured_flow_root = knowledge_cfg.get("brain_flow_root")
    if configured_flow_root:
        os.environ["BRAIN_FLOW_HOME"] = str(expand_path(configured_flow_root).resolve())
    elif os.environ.get("BRAIN_FLOW_HOME"):
        os.environ["BRAIN_FLOW_HOME"] = str(expand_path(os.environ["BRAIN_FLOW_HOME"]).resolve())
    if os.environ.get("BRAIN_PYTHON"):
        os.environ["BRAIN_PYTHON"] = str(expand_path(os.environ["BRAIN_PYTHON"]).resolve())
    credential_env = (cfg.get("auth") or {}).get("credential_env", "BRAIN_CREDENTIALS_FILE")
    if not os.environ.get("BRAIN_CREDENTIALS_FILE") and credential_env:
        configured_credentials = os.environ.get(credential_env)
        if configured_credentials:
            os.environ["BRAIN_CREDENTIALS_FILE"] = str(
                expand_path(configured_credentials).resolve())
    gateway = Gateway()
    agent = ResearchAgent(gateway, model, cfg, root / "runs",
                          stop_flag=stop_flag)
    lock = SingleWriterLock(root / "state" / "agent.lock")
    try:
        lock.__enter__()
    except WriterLockError as exc:
        print(f"research paused: {exc}", file=sys.stderr)
        return 2
    try:
        gateway.start()
        if args.command == "run-once":
            result = agent.run_once(
                args.objective, dataset_id=args.dataset,
                diagnosis_id=args.diagnosis_id, remedy_id=args.remedy_id,
                allow_remedy_round_overrun=args.allow_remedy_round_overrun)
        elif args.command == "run":
            result = agent.run_case(args.objective, args.max_batches,
                                    dataset_id=args.dataset,
                                    diagnosis_id=args.diagnosis_id, remedy_id=args.remedy_id,
                                    allow_remedy_round_overrun=args.allow_remedy_round_overrun)
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
        lock.__exit__(None, None, None)


if __name__ == "__main__":
    sys.exit(main())
