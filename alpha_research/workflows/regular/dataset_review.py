"""One evidence-checked Dataset review and, optionally, one optimization branch.

The Campaign caller owns the writer lock and per-Dataset scheduling.
This module NEVER automatically repeats an optimization after an intent exists.
It must call recover_simulation_intent on the existing intent instead.
"""
import copy
import json
from pathlib import Path
from uuid import uuid4

from alpha_research.state.store import atomic_write_json, read_json
from alpha_research.simulation.intent import recover_simulation_intent
from alpha_research.workflows.optimization.workflow import run_optimization


class DatasetReviewError(ValueError):
    pass


FINAL_STATUSES = frozenset({"complete", "no_selection"})


def _read_required(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DatasetReviewError(f"Dataset review evidence missing or unreadable: {Path(path).name}") from exc
    if not isinstance(data, dict):
        raise DatasetReviewError("Dataset review evidence must be an object")
    return data


def _save(path, review):
    atomic_write_json(path, review)
    return copy.deepcopy(review)


def _validate_directory(directory, state_root):
    root = Path(state_root).resolve()
    path = Path(directory).resolve()
    expected = root / "state" / "research_v1_1" / "campaign_runs" / "dataset_reviews"
    if not path.is_relative_to(expected.resolve()):
        raise DatasetReviewError("Dataset review directory must be inside campaign state")
    return path


def _single_batch_evidence(agent, dataset_id, receipt):
    path = Path(receipt["batch_dir"]).resolve()
    if not path.is_relative_to(Path(agent.runs_root).resolve()):
        raise DatasetReviewError("Dataset batch receipt is outside research runs")
    result = _read_required(path / "results.json")
    intent = _read_required(path / "simulation_intent.json")
    prereg = _read_required(path / "prereg.json")
    alpha_ids = receipt.get("alpha_ids")
    if (prereg.get("dataset_id") != dataset_id
            or not isinstance(alpha_ids, list) or not alpha_ids
            or intent.get("status") != "READ_BACK"
            or intent.get("alpha_ids") != alpha_ids
            or result.get("alpha_ids") != alpha_ids):
        raise DatasetReviewError("Dataset review requires matching verified simulation readback")
    if result.get("attribution_status") != "complete":
        result = agent.attribute_saved_batch(path, result)
    receipt["attribution_status"] = result.get("attribution_status", "unresolved")
    if receipt["attribution_status"] != "complete":
        return None, None, "attribution_pending"

    readback = result.get("readback")
    variants = prereg.get("variants")
    settings = intent.get("settings") or prereg.get("simulation_settings")
    if (not isinstance(readback, dict) or not isinstance(variants, list)
            or len(variants) != len(alpha_ids) or not isinstance(settings, dict)):
        raise DatasetReviewError("Dataset source metrics or effective settings are unreadable")
    attribution = result.get("attribution")
    if not isinstance(attribution, dict) or not isinstance(attribution.get("summary"), str):
        raise DatasetReviewError("Dataset attribution summary is unreadable")
    measured = {}
    for alpha_id, variant in zip(alpha_ids, variants):
        observation = readback.get(alpha_id)
        details = (observation.get("details") if isinstance(observation, dict) else None)
        metrics = (details.get("is") if isinstance(details, dict) else None)
        if not isinstance(metrics, dict):
            continue
        sharpe = metrics.get("sharpe")
        if type(sharpe) not in (int, float):
            continue
        if not isinstance(variant, dict) or not isinstance(variant.get("expression"), str):
            continue
        measured[alpha_id] = {
            "alpha_id": alpha_id,
            "batch_ref": str(path),
            "expression": variant["expression"],
            "dataset_id": dataset_id,
            "settings": copy.deepcopy(settings),
            "metrics": {name: value for name, value in metrics.items()
                        if type(value) in (int, float)},
            "gate": (result.get("gate") or {}).get(alpha_id),
        }
    return {"batch_ref": str(path), "alpha_ids": list(alpha_ids),
            "summary": attribution["summary"], "attribution": attribution}, measured, None


def _batch_evidence(agent, dataset_id, receipt):
    """Verify either a simple production batch or a multi-child field-plan batch.

    Each child owns a separate immutable simulation intent and readback.
    Never assume the parent aggregate has its own simulation intent.
    """
    root = Path(agent.runs_root).resolve()
    path = Path(receipt["batch_dir"]).resolve()
    if not path.is_relative_to(root):
        raise DatasetReviewError("Dataset batch is outside the research runs")
    parent = _read_required(path / "results.json")
    children = parent.get("child_batches")
    if children is None:
        return _single_batch_evidence(agent, dataset_id, receipt)
    if not isinstance(children, list) or not children:
        raise DatasetReviewError("field research parent has no verified child receipts")
    collected = []
    measured = {}
    all_ids = []
    pending = False
    for child in children:
        if (not isinstance(child, dict)
                or not isinstance(child.get("batch_dir"), str)
                or not isinstance(child.get("alpha_ids"), list)):
            raise DatasetReviewError("field research child receipt is malformed")
        child_path = Path(child["batch_dir"]).resolve()
        if not child_path.is_relative_to(path / "execution"):
            raise DatasetReviewError("field research child escaped its parent directory")
        group, sources, unresolved = _single_batch_evidence(agent, dataset_id, child)
        child["attribution_status"] = "unresolved" if unresolved else "complete"
        if unresolved:
            pending = True
            continue
        collected.append(group)
        all_ids.extend(group["alpha_ids"])
        if any(identifier in measured for identifier in sources):
            raise DatasetReviewError("duplicate Alpha ID across field research children")
        measured.update(sources)
    parent_ids = parent.get("alpha_ids")
    if not isinstance(parent_ids, list):
        raise DatasetReviewError("parent aggregate Alpha IDs are missing")
    if not pending and parent_ids != all_ids:
        raise DatasetReviewError("parent aggregate and child Alpha IDs differ")
    parent["child_batches"] = children
    parent["attribution_status"] = "unresolved" if pending else "complete"
    if not pending:
        parent["attribution"] = {
            "summary": "\n\n".join(group["summary"] for group in collected)
        }
    atomic_write_json(path / "results.json", parent)
    receipt["attribution_status"] = parent["attribution_status"]
    if pending:
        return None, None, "attribution_pending"
    return {
        "batch_ref": str(path),
        "alpha_ids": list(parent_ids),
        "summary": parent["attribution"]["summary"],
        "attribution": parent["attribution"],
        "child_batches": collected,
    }, measured, None


def review_dataset(agent, gateway, state_root, directory, request,
                   dataset_id, receipts, failures):
    """Persist review before optional execution; recover existing intent, never repost."""
    directory = _validate_directory(directory, state_root)
    path = directory / "review.json"
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        raise DatasetReviewError("Dataset ID required")
    if not isinstance(receipts, list) or not receipts:
        raise DatasetReviewError("Dataset review requires completed receipts")
    if not isinstance(request, dict) or not isinstance(request.get("objective"), str):
        raise DatasetReviewError("Dataset review objective is required")
    if path.exists():
        review = _read_required(path)
        if (review.get("schema_version") != 1
                or review.get("dataset_id") != dataset_id
                or review.get("objective") != request["objective"]):
            raise DatasetReviewError("saved Dataset review identity mismatch")
        if review.get("status") in FINAL_STATUSES:
            return copy.deepcopy(review)
        if review.get("status") == "optimization_pending":
            intent_ref = review.get("optimization_intent_ref")
            if not isinstance(intent_ref, str):
                raise DatasetReviewError("optimization pending without recovery target")
            intent = Path(intent_ref).resolve()
            expected = Path(state_root).resolve() / "state" / "research_v1_1" / "optimization"
            if not intent.is_relative_to(expected):
                raise DatasetReviewError("optimization recovery path is outside state root")
            if not intent.is_file():
                raise DatasetReviewError("optimization intent is missing; manual review required")
            # Genuine platform recovery reads the original saved intent,
            # not a new run_optimization call. The platform gateway controls
            # whether an interrupted POST can be resumed.
            result = recover_simulation_intent(
                gateway, state_root, intent, config=agent.config)
            if not isinstance(result, dict) or not result.get("candidate_alpha_id"):
                raise DatasetReviewError("optimization recovery did not settle the saved intent")
            review["optimization"] = result
            review["status"] = "complete"
            review.pop("error", None)
            return _save(path, review)
        if review.get("status") not in {"attribution_pending", "optimization_prepared"}:
            raise DatasetReviewError("saved Dataset review has unresolved status")
        if review["status"] == "optimization_prepared":
            # Crash after planning but before the gateway confirmed that an
            # execution started: do not guess whether it was posted.
            raise DatasetReviewError(
                "prepared optimization requires explicit operator review")
    else:
        review = {
            "schema_version": 1, "dataset_id": dataset_id,
            "objective": request["objective"], "status": "attribution_pending",
            "attribution": None, "source": None, "optimization": None,
        }
        _save(path, review)

    batch_reports = []
    sources = {}
    for receipt in receipts:
        batch, measured, pending = _batch_evidence(agent, dataset_id, receipt)
        if pending:
            review["status"] = "attribution_pending"
            review["error"] = "saved batch attribution is unresolved"
            return _save(path, review)
        batch_reports.append(batch)
        for alpha_id, row in measured.items():
            if alpha_id in sources:
                raise DatasetReviewError("Alpha ID is repeated across Dataset receipts")
            sources[alpha_id] = row
    review["status"] = "attribution_pending"
    review["receipt_refs"] = [row["batch_ref"] for row in batch_reports]
    review["failures"] = copy.deepcopy(failures or [])
    _save(path, review)

    if hasattr(agent, "_begin_session"):
        campaign_id = directory.parent.name
        agent._begin_session(f"regular:campaign:{campaign_id}")
    response = agent._decide("review_dataset_results", {
        "objective": request["objective"],
        "dataset_id": dataset_id,
        "batch_attributions": batch_reports,
        "eligible_sources": list(sources.values()),
        "failed_attempts": review["failures"],
        "ppa_profile": request.get("ppa_profile") is True,
    }, batch_dir=directory)
    if (not isinstance(response, dict)
            or not isinstance(response.get("summary"), str)
            or not response["summary"].strip()):
        raise DatasetReviewError("Dataset review requires a nonempty summary")
    selected = response.get("selection")
    if selected is None:
        review.update(status="no_selection", attribution=response, source=None)
        review.pop("error", None)
        return _save(path, review)
    reason = response.get("selection_reason")
    if (not isinstance(selected, str) or selected not in sources
            or not isinstance(reason, str) or not reason.strip()):
        raise DatasetReviewError(
            "Dataset review selection must match one measured source with a reason")
    source = sources[selected]
    scope = request
    settings = source["settings"]
    if any(scope.get(key) is not None and settings.get(key) != scope[key]
           for key in ("region", "delay", "universe")):
        raise DatasetReviewError("Dataset optimization source changed user scope")
    run_id = uuid4().hex
    intent = (Path(state_root).resolve() / "state" / "research_v1_1" /
              "optimization" / f"{run_id}_simulation_intent.json")
    review.update(
        status="optimization_prepared",
        attribution=response,
        source={"alpha_id": selected, "dataset_id": dataset_id,
                "batch_ref": source["batch_ref"], "settings": settings},
        optimization_run_id=run_id,
        optimization_intent_ref=str(intent),
    )
    _save(path, review)
    # Persist the 'started' state first: a crash cannot be mistaken for
    # permission to submit another optimization. A missing intent is BLOCKED.
    review["status"] = "optimization_pending"
    _save(path, review)
    try:
        result = run_optimization(
            gateway, agent.model, agent.config, state_root,
            alpha_id=selected, objective=request["objective"],
            region=scope.get("region"), run_id=run_id,
            expected_dataset=dataset_id, expected_settings=settings,
            ppa_profile=request.get("ppa_profile") is True,
            selection_evidence=reason, stop_flag=getattr(agent, "stop_flag", None),
        )
        if not isinstance(result, dict) or not result.get("candidate_alpha_id"):
            raise DatasetReviewError("optimization returned no verified candidate Alpha")
    except Exception as exc:
        review["error"] = f"{type(exc).__name__}: {str(exc)[:400]}"
        return _save(path, review)
    review["status"] = "complete"
    review["optimization"] = result
    review.pop("error", None)
    return _save(path, review)
