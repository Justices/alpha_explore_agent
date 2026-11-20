"""Deterministic field-catalog admission and persisted Campaign field pools."""
from random import sample

from alpha_research.progress import write_progress
from alpha_research.state.store import atomic_write_json, read_json
from alpha_research.workflows.regular.errors import ResearchError
from alpha_research.workflows.regular.scope import SCOPE_KEYS


def _rows(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("results", "result", "operators"):
            if key in payload:
                return _rows(payload[key])
    raise ResearchError("catalog response is not a readable list")


def _field_summaries(fields):
    """Keep field discovery contexts focused while preserving the full saved catalog."""
    keys = ("id", "type", "description", "coverage", "dateCoverage",
            "userCount", "alphaCount", "fieldCount", "valueScore")
    return [{key: row[key] for key in keys if key in row}
            for row in fields if isinstance(row, dict)]


def _admissible_field_rows(rows, dataset_id):
    eligible = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            continue
        ds = row.get("dataset") or {}
        ds_id = ds.get("id") if isinstance(ds, dict) else ds
        if (ds_id != dataset_id or row.get("type") not in {"MATRIX", "VECTOR"}
                or not isinstance(row.get("description"), str)
                or not row["description"].strip()):
            continue
        eligible[row["id"]] = row
    if not eligible:
        raise ResearchError("selected Dataset has no readable fields")
    return eligible


def _catalog(rows, dataset_id):
    return {field_id: {"type": row["type"], "dataset": dataset_id}
            for field_id, row in _admissible_field_rows(rows, dataset_id).items()}


def _passes_field_prefilter(row):
    coverage, date_coverage = row.get("coverage"), row.get("dateCoverage")
    alpha_count = row.get("alphaCount")
    return (type(coverage) in (int, float) and 0.5 <= coverage <= 1
            and type(date_coverage) in (int, float) and 0.5 <= date_coverage <= 1
            and type(alpha_count) is int and 0 <= alpha_count <= 800)


def _campaign_field_pool(path, fields, dataset_id, scope):
    admissible = _admissible_field_rows(fields, dataset_id)
    eligible = {field_id: row for field_id, row in admissible.items()
                if _passes_field_prefilter(row)}
    identity = {"dataset_id": dataset_id, "scope": {key: scope[key] for key in SCOPE_KEYS}}
    saved = None
    if path.exists():
        saved = read_json(path)
        if not isinstance(saved, dict) or any(saved.get(key) != value
                                               for key, value in identity.items()):
            raise ResearchError("saved Campaign field pool does not match the Dataset scope")
        field_ids = saved.get("field_ids")
        if (not isinstance(field_ids, list) or not 1 <= len(field_ids) <= 100
                or any(not isinstance(key, str) or key not in admissible for key in field_ids)
                or len(set(field_ids)) != len(field_ids)):
            raise ResearchError("saved Campaign field pool cannot be verified against live fields")
    if not eligible:
        write_progress(f"{dataset_id} · {len(admissible)} 个可读字段中，没有字段满足覆盖率和 Alpha 数量预筛选")
        raise ResearchError("no fields pass instrument coverage >= 50%, date coverage >= 50%, and Alpha count <= 800")
    if saved is not None and all(field_id in eligible for field_id in saved["field_ids"]):
        field_ids = saved["field_ids"]
        selection = "复用已保存的字段池"
    else:
        field_ids = list(eligible)
        selection = "使用全部预筛通过的字段"
        if len(field_ids) > 100:
            field_ids = sample(field_ids, 100)
            selection = "预筛后随机抽样"
        if saved is not None:
            selection = f"重新筛选字段池 · {selection}"
    pool = {**identity, "eligible_count": len(eligible), "field_ids": field_ids}
    if saved != pool:
        atomic_write_json(path, pool)
    write_progress(f"{dataset_id} · {selection} · 目录 {len(fields)} 条，可读 {len(admissible)} 条，"
                   f"预筛通过 {len(eligible)} 条，"
                   f"选入 {len(field_ids)} 条 · {path}")
    return [eligible[field_id] for field_id in field_ids]
