"""Discover Regular Campaign scope from live BRAIN catalogs.

The caller owns the platform session and sets its case before catalog reads.
Discovery is read-only and never chooses simulation defaults.
"""

from datetime import datetime, timezone


class ScopeDiscoveryError(ValueError):
    pass


SCOPE_KEYS = ("region", "delay", "universe")


def exact_catalog_scope(row, scope):
    if isinstance(row, dict) and all(row.get(key) == scope[key] for key in SCOPE_KEYS):
        return {key: row[key] for key in SCOPE_KEYS}
    return None


def dataset_category(value):
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict):
        return value.get("id") or value.get("name")
    return None


def category_matches(value, expected):
    labels = (value.get("id"), value.get("name")) if isinstance(value, dict) else (value,)
    return isinstance(expected, str) and any(
        isinstance(label, str) and label.strip().casefold() == expected.strip().casefold()
        for label in labels)


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
        catalogs.append({"scope": scope, "count": len(catalog)})
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
            category_value = catalog_row.get("category")
            actual_category = dataset_category(category_value)
            evidence = exact_catalog_scope(catalog_row, scope)
            source = "exact_catalog"
            detail = None
            if evidence is None or (category is not None and actual_category is None):
                if dataset_id not in detail_cache:
                    detail_cache[dataset_id] = gateway.call(
                        "get_dataset_details", dataset_id=dataset_id)
                detail = detail_cache[dataset_id]
                if not isinstance(detail, dict) or detail.get("id") != dataset_id:
                    unresolved.append({"dataset_id": dataset_id, "scope": scope,
                                       "reason": "dataset_detail_unreadable"})
                    continue
                if dataset_category(detail.get("category")) is not None:
                    category_value = detail["category"]
                    actual_category = dataset_category(category_value)
                if evidence is None:
                    data = detail.get("data")
                    if isinstance(data, list):
                        evidence = next((item for item in data
                                         if exact_catalog_scope(item, scope)), None)
                    source = "dataset_detail"
            if category is not None and actual_category is None:
                unresolved.append({"dataset_id": dataset_id, "scope": scope,
                                   "reason": "category_unverified"})
                continue
            if category is not None and not category_matches(category_value, category):
                continue
            if evidence is None:
                unresolved.append({"dataset_id": dataset_id, "scope": scope,
                                   "reason": "dataset_scope_unverified"})
                continue
            discovered.setdefault(dataset_id, {
                "dataset_id": dataset_id, "category": actual_category,
                "combinations": []})["combinations"].append({
                    "region": region, "delay": current_delay,
                    "universe": current_universe, "detail_scope": evidence,
                    "scope_evidence_source": source})
    if not discovered and not unresolved:
        unresolved.append({"reason": "no_eligible_dataset_confirmed"})
    return {"region": region, "requested": {
                "dataset_ids": dataset_ids, "category": category,
                "delay": delay, "universe": universe},
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "datasets": [discovered[key] for key in sorted(discovered)],
            "unresolved": unresolved, "catalogs": catalogs,
            "coverage_complete": not unresolved}
