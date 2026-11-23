"""Knowledge and evidence validity operations with a single writer lock.

All mutations run through KnowledgeService; no BRAIN or model session is
created by these commands.
"""
import json
import sys

from alpha_research.knowledge import KnowledgeService
from alpha_research.state.writer_lock import SingleWriterLock

KNOWLEDGE_COMMANDS = frozenset(("knowledge", "evidence-validity"))


def run_knowledge_command(args, root):
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
