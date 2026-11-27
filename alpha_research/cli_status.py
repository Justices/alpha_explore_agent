"""Local status projection without model, BRAIN, or provider transport.

State reads remain observational and preserve the CLI's existing JSON shape.
"""
import json
import re

from alpha_research.config import DEFAULT_CONFIG
from alpha_research.state.paths import CATALOG_SNAPSHOT_ROOT
from alpha_research.state.records import ResearchRecordStore
from alpha_research.qualification.pool import CandidatePool, policy_hash
from alpha_research.workflows.regular.campaign import campaign_can_resume


def run_status_command(args, root, stop_flag):
    config_path = args.config or root / "config" / "nastu_quant.json"
    config_status = "loaded"
    # Gate and Preferred policies are application-owned. Scope-only config
    # edits must not make existing assessments appear stale or redefine the
    # thresholds displayed by the workbench.
    current_policy = policy_hash(DEFAULT_CONFIG)
    try:
        pool_config = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(pool_config, dict):
            raise ValueError("configuration must be an object")
    except FileNotFoundError:
        config_status = "missing"
    except (OSError, ValueError, TypeError):
        config_status = "unreadable"
    qualification_policy = {
        "status": "available",
        "version": "preferred-v1",
        "candidate": DEFAULT_CONFIG["gate"],
        "preferred": DEFAULT_CONFIG["preferred"],
        "policy_sha256": current_policy,
    }
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
    v11_campaigns = []
    v11_root = root / "state" / "research_v1_1" / "campaign_runs"
    if v11_root.exists():
        for path in sorted(v11_root.glob("*.json")):
            if not re.fullmatch(r"[0-9a-f]{32}\.json", path.name):
                continue
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("deleted_at"):
                    continue
                v11_campaigns.append({
                    "campaign_id": saved.get("campaign_id"),
                    "status": saved.get("status"),
                    "batches_completed": len(saved.get("completed", [])),
                    "batches_attempted": saved.get("budget", {}).get("used", {}).get("batches"),
                    "subtasks": saved.get("subtasks", []),
                    "active_item": saved.get("active_item"),
                    "resumable": campaign_can_resume(saved),
                    "has_summary": path.with_name(f"{path.stem}_summary.json").is_file(),
                    "path": str(path)})
            except (OSError, ValueError):
                v11_campaigns.append({"path": str(path), "status": "unreadable"})
    def _read_optimization_snapshot(snapshot_ref):
        if not isinstance(snapshot_ref, str) or not snapshot_ref:
            return {}
        try:
            snapshot_path = Path(snapshot_ref)
            snapshot_root = (root / "state" / "research_v1_1" /
                             "optimization" / "snapshots").resolve()
            resolved = snapshot_path.resolve()
            if (snapshot_path.is_symlink() or not resolved.is_relative_to(snapshot_root)
                    or not resolved.is_file() or resolved.stat().st_size > 5 * 1024 * 1024):
                return {}
            value = json.loads(resolved.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _metric_snapshot(details):
        block = details.get("is") if isinstance(details.get("is"), dict) else {}
        keys = ("sharpe", "fitness", "turnover", "returns", "drawdown", "margin", "weight")
        return {key: block[key] for key in keys
                if isinstance(block.get(key), (int, float))
                and not isinstance(block[key], bool)}

    def _record_summaries(kind):
        storage_kind = {"candidate_evaluations": "candidate",
                        "preferred_evaluations": "preferred"}.get(kind, kind)
        folder = root / "state" / "research_v1_1" / storage_kind
        output = []
        if folder.exists():
            for record_path in sorted(folder.glob("*.json")):
                try:
                    item = json.loads(record_path.read_text(encoding="utf-8"))
                    if kind != "candidate_evaluations" and storage_kind == "candidate" and item.get("status") != "CANDIDATE":
                        continue
                    if kind != "preferred_evaluations" and storage_kind == "preferred" and item.get("status") != "PREFERRED":
                        continue
                    policy_current = (current_policy is not None and
                                      item.get("policy_sha256") == current_policy)
                    candidate_status = item.get("candidate_status") or (
                        item.get("status") if storage_kind == "candidate" else None)
                    preferred_status = item.get("preferred_status") or (
                        item.get("status") if storage_kind == "preferred" else None)
                    if storage_kind == "candidate" and item.get("status") == "CANDIDATE" and not policy_current:
                        candidate_status = "POLICY_STALE"
                        preferred_status = "NOT_CANDIDATE"
                    if storage_kind == "preferred" and item.get("status") == "PREFERRED" and not policy_current:
                        preferred_status = "NOT_CANDIDATE"
                    extra = {}
                    if storage_kind == "optimization_source":
                        source_details = _read_optimization_snapshot(item.get("snapshot_ref"))
                        extra = {
                            "source_metrics": _metric_snapshot(source_details),
                            "source_settings": source_details.get("settings", {}),
                            "source_definition": {
                                "regular": source_details.get("regular"),
                                "selection": source_details.get("selection"),
                                "combo": source_details.get("combo"),
                            },
                            "source_snapshot_ref": item.get("snapshot_ref"),
                        }
                    elif storage_kind == "candidate_branch":
                        source_details = _read_optimization_snapshot(
                            item.get("source_snapshot_ref"))
                        source_metrics = _metric_snapshot(source_details)
                        candidate_record = {}
                        candidate_folder = root / "state" / "research_v1_1" / "candidate"
                        if candidate_folder.is_dir() and not candidate_folder.is_symlink():
                            for candidate_path in candidate_folder.glob("*.json"):
                                try:
                                    if candidate_path.is_symlink() or candidate_path.stat().st_size > 1024 * 1024:
                                        continue
                                    candidate_value = json.loads(
                                        candidate_path.read_text(encoding="utf-8"))
                                    if (candidate_value.get("candidate_ref") == item.get("branch_id")
                                            and candidate_value.get("alpha_id") == item.get("candidate_alpha_id")):
                                        candidate_record = candidate_value
                                        break
                                except (OSError, ValueError, AttributeError):
                                    continue
                        candidate_metrics = candidate_record.get("metrics", {})
                        metric_deltas = {
                            key: candidate_metrics[key] - value
                            for key, value in source_metrics.items()
                            if isinstance(candidate_metrics.get(key), (int, float))
                            and not isinstance(candidate_metrics.get(key), bool)
                        }
                        extra = {
                            "objective": item.get("objective"),
                            "rationale": item.get("rationale"),
                            "changes": item.get("changes", []),
                            "source_metrics": source_metrics,
                            "candidate_metrics": candidate_metrics,
                            "metric_deltas": metric_deltas,
                            "source_settings": source_details.get("settings", {}),
                            "candidate_settings": item.get("effective_settings", {}),
                            "candidate_failures": candidate_record.get("failures", []),
                            "candidate_pending": candidate_record.get("pending", []),
                            "source_snapshot_ref": item.get("source_snapshot_ref"),
                        }
                    output.append({
                        "id": item.get("candidate_id") or item.get("run_id") or item.get("branch_id") or
                              item.get("source_id"),
                        "source_alpha_id": item.get("source_alpha_id") or item.get("alpha_id"),
                        "workflow": item.get("workflow"),
                        **extra,
                        "candidate_status": candidate_status,
                        "preferred_status": preferred_status,
                        "policy_current": policy_current,
                        "candidate_alpha_id": item.get("candidate_alpha_id"),
                        "status": item.get("status"),
                        "simulation_intent_ref": item.get("simulation_intent_ref"),
                        "response_ref": item.get("response_ref"),
                        "receipt_ref": item.get("receipt_ref") or item.get("readback_ref"),
                        "audit_ref": item.get("audit_ref"),
                        "report_ref": item.get("report_ref"),
                        "iteration_session_id": item.get("iteration_session_id"),
                        "iterations_completed": item.get("iterations_completed"),
                        "iteration_budget": item.get("iteration_budget"),
                        "iteration_stop_reason": item.get("iteration_stop_reason"),
                        "iteration_ref": item.get("iteration_ref"),
                        "proposal_sha256": item.get("proposal_sha256"),
                        "candidate_definition_sha256": item.get("candidate_definition_sha256"),
                        "eligible_alpha_count": item.get("eligible_alpha_count"),
                        "unresolved_count": item.get("unresolved_count"),
                        "unresolved": item.get("unresolved"),
                        "ra_fail_count": (item.get("gate_evidence") or {}).get("ra_fail_count")
                            if isinstance(item.get("gate_evidence"), dict)
                            else item.get("ra_fail_count"),
                        "failures": item.get("failures", []),
                        "pending": item.get("pending", []),
                        "metrics": item.get("metrics", {}),
                        "correlations": item.get("correlations", {}),
                        "gate_evidence": item.get("gate_evidence", {}),
                        "thresholds": item.get("thresholds", {}),
                        "qualified": item.get("qualified"),
                        "evidence_refs": item.get("evidence_refs", {}),
                        "evidence_sha256": item.get("evidence_sha256"),
                        "policy_sha256": item.get("policy_sha256"),
                        "source_ref": item.get("source_ref"),
                        "candidate_ref": item.get("candidate_ref"),
                        "created_at": item.get("created_at"),
                        "updated_at": item.get("updated_at"),
                        "simulation_invoked": item.get("simulation_invoked"),
                        "alpha_submission_invoked": item.get("alpha_submission_invoked"),
                        "source_unchanged_verified": item.get("source_unchanged_verified"),
                        "path": str(record_path)})
                except (OSError, ValueError):
                    output.append({"path": str(record_path), "status": "unreadable"})
        output.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return output[:200]
    try:
        if current_policy is None:
            raise ValueError("current admission policy is unavailable")
        global_pool = CandidatePool(ResearchRecordStore(root)).build_index(
            current_policy_sha256=current_policy)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        global_pool = {"status": "unavailable", "reason": type(exc).__name__}
    print(json.dumps({"global_alpha_pool": global_pool,
                      "qualification_policy": qualification_policy,
                      "research_config": {"status": config_status, "path": str(config_path)},
                      "catalog_snapshot_root": str(CATALOG_SNAPSHOT_ROOT),
                      "stopped": stop_flag.exists(),
                      "unfinished_batches": unfinished,
                      "campaigns": campaigns,
                      "v11_campaigns": v11_campaigns,
                      "v11_super_runs": _record_summaries("super_run"),
                      "v11_optimization_sources": _record_summaries("optimization_source"),
                      "v11_candidate_branches": _record_summaries("candidate_branch"),
                      "v11_candidates": [
                          row for row in global_pool.get("entries", [])
                          if row["candidate_status"] == "CANDIDATE"],
                      "v11_preferred": [
                          row for row in global_pool.get("entries", [])
                          if row["candidate_status"] == "CANDIDATE" and
                             row["preferred_status"] == "PREFERRED"],
                      "v11_candidate_evaluations": _record_summaries("candidate_evaluations"),
                      "v11_preferred_evaluations": _record_summaries("preferred_evaluations")},
                     ensure_ascii=False))
    return 0
