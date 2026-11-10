"""Discover Regular Campaign scope from live BRAIN catalogs.

The caller owns the platform session and sets its case before catalog reads.
Discovery is read-only and never chooses simulation defaults.
"""

from datetime import datetime, timezone


class ScopeDiscoveryError(ValueError):
    pass


def _category(value):
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        return value.get("id") or value.get("name")
    return None


def _rows(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and isinstance(value.get("results"), list):
        return value["results"]
    raise ScopeDiscoveryError("platform catalog is unreadable")


def discover_regular_scope(gateway, *, region, dataset_ids=None, category=None,
                           delay=None, universe=None):
    """Return every proven eligible Dataset/Delay/Universe combination.

    A supplied Dataset or Category whose eligibility cannot be proved is
    unresolved, never substituted with a different Dataset.
    """
    if not isinstance(region, str) or not region:
        raise ScopeDiscoveryError("Region is required")
    if dataset_ids is not None:
        if (not isinstance(dataset_ids, list) or not dataset_ids or
                any(not isinstance(item, str) or not item for item in dataset_ids) or
                len(set(dataset_ids)) != len(dataset_ids)):
            raise ScopeDiscoveryError("dataset_ids must be a nonempty unique list")
    options = gateway.call("get_platform_setting_options")
    rows = options.get("instrument_options") if isinstance(options, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ScopeDiscoveryError("platform settings are unavailable")
    settings = []
    for row in rows:
        if not isinstance(row, dict) or row.get("InstrumentType") != "EQUITY":
            continue
        if row.get("Region") != region:
            continue
        current_delay = row.get("Delay")
        if delay is not None and current_delay != delay:
            continue
        universes = row.get("Universe")
        if not isinstance(universes, list):
            raise ScopeDiscoveryError("platform Universe choices are unreadable")
        for current_universe in universes:
            if universe is None or current_universe == universe:
                settings.append((current_delay, current_universe))
    settings = sorted(set(settings), key=lambda item: (str(item[0]), item[1]))
    if not settings:
        raise ScopeDiscoveryError("no platform-valid settings match the requested scope")

    discovered = {}
    unresolved = []
    catalogs = []
    detail_cache = {}
    for current_delay, current_universe in settings:
        scope = {"instrument_type": "EQUITY", "region": region,
                 "delay": current_delay, "universe": current_universe}
        catalog = _rows(gateway.call("get_datasets", **scope))
        catalogs.append({"scope": scope, "rows": catalog})
        by_id = {item.get("id"): item for item in catalog
                 if isinstance(item, dict) and isinstance(item.get("id"), str)}
        selected_ids = dataset_ids if dataset_ids is not None else sorted(by_id)
        for dataset_id in selected_ids:
            catalog_row = by_id.get(dataset_id)
            if catalog_row is None:
                if dataset_ids is not None:
                    unresolved.append({"dataset_id": dataset_id, "scope": scope,
                                       "reason": "not_in_exact_scope_catalog"})
                continue
            if dataset_id not in detail_cache:
                detail_cache[dataset_id] = gateway.call(
                    "get_dataset_details", dataset_id=dataset_id)
            detail = detail_cache[dataset_id]
            if not isinstance(detail, dict) or detail.get("id") != dataset_id:
                unresolved.append({"dataset_id": dataset_id, "scope": scope,
                                   "reason": "dataset_detail_unreadable"})
                continue
            actual_category = _category(detail.get("category")) or _category(
                catalog_row.get("category"))
            if category is not None and actual_category is None:
                unresolved.append({"dataset_id": dataset_id, "scope": scope,
                                   "reason": "category_unverified"})
                continue
            if category is not None and actual_category != category:
                continue
            data = detail.get("data")
            if not isinstance(data, list):
                unresolved.append({"dataset_id": dataset_id, "scope": scope,
                                   "reason": "dataset_scope_detail_unreadable"})
                continue
            matches = [item for item in data if isinstance(item, dict)
                       and item.get("region") == region
                       and item.get("delay") == current_delay
                       and item.get("universe") == current_universe]
            if not matches:
                if dataset_ids is not None:
                    unresolved.append({"dataset_id": dataset_id, "scope": scope,
                                       "reason": "detail_does_not_confirm_scope"})
                continue
            discovered.setdefault(dataset_id, {
                "dataset_id": dataset_id, "category": actual_category,
                "catalog_row": catalog_row, "detail": detail,
                "combinations": []})["combinations"].append({
                    "region": region, "delay": current_delay,
                    "universe": current_universe, "detail_scope": matches[0]})
    if not discovered and not unresolved:
        unresolved.append({"reason": "no_eligible_dataset_confirmed"})
    return {"region": region, "requested": {
                "dataset_ids": dataset_ids, "category": category,
                "delay": delay, "universe": universe},
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "datasets": [discovered[key] for key in sorted(discovered)],
            "unresolved": unresolved, "catalogs": catalogs,
            "coverage_complete": not unresolved}
