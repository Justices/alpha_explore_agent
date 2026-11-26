"""Summaries of completed Campaign evidence for review and later research."""

from collections import Counter
from pathlib import Path

from alpha_research.state.store import atomic_write_json, read_json


def write_campaign_summary(state, path):
    completed = {row["item_index"]: row for row in state["completed"]}
    failures = {row["item_index"]: row for row in state.get("failures", [])}
    rows = []
    for index, item in enumerate(state["items"]):
        receipt = completed.get(index) or failures.get(index)
        if receipt is None:
            continue
        row = {**item, "status": "completed" if index in completed else "unresolved"}
        batch_dir = receipt.get("batch_dir") or receipt.get("batch_ref")
        result = read_json(Path(batch_dir) / "results.json", default={}) if batch_dir else {}
        children = result.get("child_batches") or ([{"batch_dir": batch_dir}] if result else [])
        gates, checks, measured, attribution = Counter(), Counter(), [], []
        for child in children:
            child_result = read_json(Path(child["batch_dir"]) / "results.json", default={})
            prereg = read_json(Path(child["batch_dir"]) / "prereg.json", default={})
            expressions = {alpha_id: variant.get("expression") for alpha_id, variant in
                           zip(child_result.get("alpha_ids", []), prereg.get("variants", []))}
            for alpha_id, gate in child_result.get("gate", {}).items():
                gates[gate["status"]] += 1
                checks.update(gate.get("failures", []))
                metrics = child_result.get("readback", {}).get(alpha_id, {}).get("details", {}).get("is") or {}
                if type(metrics.get("sharpe")) in (int, float):
                    measured.append({"alpha_id": alpha_id, "expression": expressions.get(alpha_id),
                                     "sharpe": metrics["sharpe"],
                                     "fitness": metrics.get("fitness"), "gate": gate["status"]})
            analysis = child_result.get("attribution")
            if isinstance(analysis, dict) and analysis.get("summary"):
                attribution.append(analysis["summary"])
        row.update(alpha_count=len(receipt.get("alpha_ids", [])), gate_counts=dict(gates),
                   common_failures=dict(checks.most_common(5)),
                   best_sharpe=max(measured, key=lambda entry: entry["sharpe"], default=None),
                   attribution=attribution, attribution_status=result.get("attribution_status"),
                   field_failures=result.get("field_failures", []),
                   execution_failures=result.get("execution_failures", []))
        if receipt.get("reason"):
            row["reason"] = receipt["reason"]
        rows.append(row)
    report = {"campaign_id": state["campaign_id"], "objective": state["request"]["objective"],
              "status": state["status"], "updated_at": state["updated_at"],
              "dataset_reviews": state.get("dataset_reviews", {}),
              "total_combinations": len(state["items"]), "combinations": rows,
              "alpha_count": sum(row["alpha_count"] for row in rows)}
    atomic_write_json(path, report)
    return report


def current_campaign_findings(state, item, limit=3):
    """Summarize recent completed rounds from this Campaign and exact scope."""
    if type(limit) is not int or limit < 1:
        raise ValueError("current Campaign feedback limit must be positive")
    keys = ("dataset_id", "region", "delay", "universe")
    target = tuple(item.get(key) for key in keys)
    rows = []
    items = state.get("items", [])
    for receipt in state.get("completed", []):
        index = receipt.get("item_index")
        if type(index) is not int or not 0 <= index < len(items):
            continue
        saved_item = items[index]
        if tuple(saved_item.get(key) for key in keys) != target:
            continue
        batch_ref = receipt.get("batch_dir")
        if not isinstance(batch_ref, str) or not batch_ref:
            continue
        batch = Path(batch_ref)
        result = read_json(batch / "results.json", default={})
        gate_counts, failures, measured = Counter(), Counter(), []
        for alpha_id, verdict in (result.get("gate") or {}).items():
            if not isinstance(verdict, dict):
                continue
            gate_counts[verdict.get("status", "UNKNOWN")] += 1
            failures.update(item for item in verdict.get("failures", [])
                            if isinstance(item, str))
            metrics = (((result.get("readback") or {}).get(alpha_id) or {})
                       .get("details", {}).get("is") or {})
            sharpe = metrics.get("sharpe")
            if type(sharpe) in (int, float):
                measured.append({"sharpe": sharpe, "fitness": metrics.get("fitness")})
        attribution = result.get("attribution")
        summary = attribution.get("summary") if isinstance(attribution, dict) else None
        rows.append({
            "campaign_id": state.get("campaign_id"),
            "iteration": receipt.get("completed_at"),
            "objective": state.get("request", {}).get("objective"),
            "dataset_id": saved_item.get("dataset_id"),
            "alpha_count": len(receipt.get("alpha_ids", [])),
            "gate_counts": dict(gate_counts),
            "common_failures": dict(failures.most_common(5)),
            "best_metrics": max(measured, key=lambda row: row["sharpe"], default=None),
            "attribution_status": result.get("attribution_status"),
            "attribution": summary[:350] if isinstance(summary, str) else None,
            "evidence_ref": batch_ref,
        })
    rows.sort(key=lambda row: row.get("iteration") or "", reverse=True)
    return rows[:limit]


def prior_campaign_findings(directory, current_id, items):
    scope_keys = ("dataset_id", "region", "delay", "universe")
    history = {tuple(item[key] for key in scope_keys): [] for item in items}
    reports = (read_json(path, default={}) for path in directory.glob("*_summary.json")
               if path.name != f"{current_id}_summary.json")
    for report in sorted((row for row in reports if isinstance(row, dict)),
                         key=lambda row: row.get("updated_at", ""), reverse=True):
        for row in report.get("combinations", []):
            matches = history.get(tuple(row.get(key) for key in scope_keys))
            if matches is None or len(matches) >= 3:
                continue
            dataset_summary = (report.get("dataset_reviews", {}).get(row["dataset_id"], {})
                               .get("attribution") or {}).get("summary")
            attribution = ([dataset_summary] if dataset_summary else []) + row["attribution"]
            matches.append({
                "campaign_id": report["campaign_id"], "objective": report["objective"],
                "status": row["status"], "alpha_count": row["alpha_count"],
                "gate_counts": row["gate_counts"], "common_failures": row["common_failures"],
                "best_sharpe": row["best_sharpe"], "attribution_status": row["attribution_status"],
                "attribution": [text[:350] for text in attribution[:2]],
            })
    return history
