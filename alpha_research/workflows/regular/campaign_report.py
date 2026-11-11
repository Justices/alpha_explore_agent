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
              "total_combinations": len(state["items"]), "combinations": rows,
              "alpha_count": sum(row["alpha_count"] for row in rows)}
    atomic_write_json(path, report)
    return report


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
            matches.append({
                "campaign_id": report["campaign_id"], "objective": report["objective"],
                "status": row["status"], "alpha_count": row["alpha_count"],
                "gate_counts": row["gate_counts"], "common_failures": row["common_failures"],
                "best_sharpe": row["best_sharpe"], "attribution_status": row["attribution_status"],
                "attribution": [text[:350] for text in row["attribution"][:2]],
            })
    return history
