"""One recoverable research batch driven by an interchangeable JSON model.

The model proposes economic decisions. Catalog admission, expression checks,
pre-registration, platform access and evidence persistence remain deterministic.
"""
from datetime import datetime
from pathlib import Path
from uuid import uuid4
import hashlib
import math
import re

from .backtest_cache import BacktestCache, BacktestCacheError
from .state_store import atomic_write_json, lock_prereg, verify_prereg
from .validator import regular_operator_names, validate
from .recovery import match_alphas_detailed
from .gatekeeper import evaluate_candidate, local_self_correlation_state, parse_correlations
from .ppa import (PPA_MAX_FIELDS, PPA_MAX_OPERATORS, PPA_MIN_SHARPE,
                  ppa_gate_kwargs, ppa_requested)
from .audit_policy import reconcile_audit_evidence
from .config import DEFAULT_CONFIG
from .campaign import CampaignStore
from .diagnosis import DiagnosisError, diagnose_batch
from .knowledge import KnowledgeService
from .model_client import DecisionRecorder
import json


class ResearchError(RuntimeError):
    pass


FIELD_EVIDENCE_KEYS = {
    "nominal_coverage": "coverage",
    "usable_observations": "zeroFraction",
    "refresh_rate": "updateFrequency",
    "scale_units": "units",
    "typical_level": "median",
    "distribution": "distribution",
}


def _duration_parts(value):
    """Parse year/month/day ISO durations returned by the platform schema."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"P(?:(\d+)Y)?(?:(\d+)M)?(?:(\d+)D)?", value)
    if not match or not any(part is not None for part in match.groups()):
        return None
    try:
        years, months, days = (int(part or 0) for part in match.groups())
    except ValueError:
        return None
    return years * 12 + months, days


def _duration_difference_bounds(left, right):
    """Bound calendar-month differences without guessing their exact day counts."""
    months, days = left[0] - right[0], left[1] - right[1]
    return (months * (28 if months >= 0 else 31) + days,
            months * (31 if months >= 0 else 28) + days)


def _validate_test_period(value, rule):
    """Accept only periods demonstrably within live choices or duration bounds."""
    if not isinstance(rule, dict) or not isinstance(value, str) or not value:
        raise ResearchError("test_period is not verified by the live settings schema")
    choices = rule.get("choices")
    if isinstance(choices, list):
        if (not choices or any(not isinstance(item, dict) or
                               not isinstance(item.get("value"), str) for item in choices)
                or value not in [item["value"] for item in choices]):
            raise ResearchError("test_period is not supported by the live settings schema")
        return {"source": "platform_settings_schema", "value": value,
                "choices": choices}
    if rule.get("type") != "string":
        raise ResearchError("test_period is not verified by the live settings schema")
    minimum, maximum = rule.get("minValue"), rule.get("maxValue")
    candidate, lower, upper = map(_duration_parts, (value, minimum, maximum))
    if (candidate is None or lower is None or upper is None
            or _duration_difference_bounds(candidate, lower)[0] < 0
            or _duration_difference_bounds(candidate, upper)[1] > 0):
        raise ResearchError("test_period is not demonstrably within the live settings range")
    return {"source": "platform_settings_schema", "value": value,
            "minValue": minimum, "maxValue": maximum}


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


def _catalog(rows, dataset_id):
    catalog = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        ds = row.get("dataset") or {}
        ds_id = ds.get("id") if isinstance(ds, dict) else ds
        if (ds_id != dataset_id or row.get("type") not in {"MATRIX", "VECTOR"}
                or not isinstance(row.get("description"), str)
                or not row["description"].strip()):
            continue
        catalog[row["id"]] = {"type": row.get("type"), "dataset": dataset_id}
    if not catalog:
        raise ResearchError("selected Dataset has no readable fields")
    return catalog


class ResearchAgent:
    def __init__(self, gateway, model, config: dict, runs_root: Path,
                 stop_flag: Path = None):
        self.gateway = gateway
        self.model = model
        self.config = config
        self.runs_root = Path(runs_root)
        self.state_root = self.runs_root.resolve().parent / "state"
        self.stop_flag = Path(stop_flag) if stop_flag else None
        self.knowledge = KnowledgeService(self.state_root, self.runs_root, config)
        self.recorder = DecisionRecorder(model, self.state_root)
        self.campaigns = CampaignStore(self.state_root)
        cache_path = self.runs_root / "backtest_cache.cache"
        cache_missing = not cache_path.exists()
        self.backtest_cache = BacktestCache(cache_path)
        try:
            if cache_missing or self.backtest_cache.needs_rebuild:
                self.backtest_cache.sync_closed_runs(self.runs_root,
                                                     self._platform_settings)
        except BacktestCacheError as exc:
            raise ResearchError(f"backtest cache initialization failed: {exc}") from exc
        self._session_packs = []
        if hasattr(gateway, "alpha_lookup"):
            gateway.alpha_lookup = self._lookup_alpha_state

    def _begin_session(self):
        session_id = self.recorder.begin_session()
        self._session_packs = []
        return session_id

    def _save_knowledge_pack(self, pack, batch_dir=None):
        raw = json.dumps(pack, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        pack_id = digest[:20]
        path = self.state_root / "logs" / "knowledge_packs" / f"{pack_id}.json"
        if not path.exists():
            atomic_write_json(path, pack)
        ref = {"pack_id": pack_id, "sha256": digest, "source_ref": str(path),
               "session_id": self.recorder.session_id}
        self._session_packs.append(ref)
        if batch_dir is not None:
            self._write_batch_knowledge_packs(batch_dir)
        return ref

    def _write_batch_knowledge_packs(self, batch_dir):
        batch_dir = Path(batch_dir)
        batch_dir.mkdir(parents=True, exist_ok=True)
        existing = (json.loads((batch_dir / "knowledge_pack.json").read_text(encoding="utf-8"))
                    if (batch_dir / "knowledge_pack.json").exists() else {})
        packs_by_id = {row.get("pack_id"): row for row in existing.get("packs", [])
                       if isinstance(row, dict) and row.get("pack_id")}
        for ref in self._session_packs:
            try:
                pack = json.loads(Path(ref["source_ref"]).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ResearchError("knowledge pack reference is unreadable") from exc
            packs_by_id.setdefault(ref["pack_id"], {**ref, "pack": pack})
        atomic_write_json(batch_dir / "knowledge_pack.json", {
            "schema_version": 1,
            "session_ids": sorted({row.get("session_id") for row in packs_by_id.values()
                                   if row.get("session_id")}),
            "packs": list(packs_by_id.values())})

    def _decide(self, task, context, batch_dir=None):
        pack = self.knowledge.build_pack(task, context)
        ref = self._save_knowledge_pack(pack, batch_dir)
        if batch_dir is not None:
            self.recorder.bind_batch(batch_dir)
        enriched = {**context, "knowledge_pack": pack}
        return self.recorder.complete_json(task, enriched,
                                           knowledge_pack_ref=ref["source_ref"])

    def _lookup_alpha_state(self, alpha_id):
        path = self.runs_root / "alpha_states.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8")).get(alpha_id)

    def _assert_gate_config(self):
        configured = self.config.get("gate")
        if not isinstance(configured, dict):
            raise ResearchError("gate configuration is missing or invalid")
        legacy_defaults = {"risk_neutralized_sharpe_min", "risk_neutralized_fitness_min"}
        values = {}
        for name, default in DEFAULT_CONFIG["gate"].items():
            value = configured.get(name, default if name in legacy_defaults else None)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ResearchError(f"invalid gate threshold: {name}")
            try:
                finite = math.isfinite(value)
            except OverflowError:
                finite = False
            if not finite:
                raise ResearchError(f"invalid gate threshold: {name}")
            values[name] = value
        for name in ("turnover_min", "turnover_max", "prod_corr_max",
                     "self_corr_max", "pairwise_corr_max"):
            if not 0 <= values[name] <= 1:
                raise ResearchError(f"invalid gate threshold: {name}")
        if values["turnover_min"] > values["turnover_max"]:
            raise ResearchError("turnover_min exceeds turnover_max")

    def _assert_auth_mode(self):
        mode = (self.config.get("auth") or {}).get("mode", "auto")
        if mode != "auto":
            raise ResearchError(f"authorization mode {mode} is not implemented for unattended execution")

    def _register_alpha_state(self, alpha_id, state):
        path = self.runs_root / "alpha_states.json"
        states = (json.loads(path.read_text(encoding="utf-8"))
                  if path.exists() else {})
        states[alpha_id] = state
        atomic_write_json(path, states)

    def _case_path(self, objective, scope):
        key = hashlib.sha256(json.dumps({
            "objective": objective, "region": scope["region"],
            "universe": scope["universe"], "delay": scope["delay"]},
            sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
        return self.runs_root / "cases" / f"{key}.json"

    def _resolve_remedy_reference(self, objective, diagnosis_id, remedy_id,
                                  allow_round_overrun=False):
        if (diagnosis_id is None) != (remedy_id is None):
            raise ResearchError("a remedy batch requires both diagnosis_id and remedy_id")
        if diagnosis_id is None:
            return None
        matches = []
        for path in self.runs_root.rglob("diagnosis.json") if self.runs_root.exists() else []:
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if row.get("diagnosis_id") == diagnosis_id:
                matches.append((path.resolve(), row))
        if len(matches) != 1:
            raise ResearchError("diagnosis_id must identify exactly one local diagnosis")
        diagnosis_path, diagnosis = matches[0]
        if diagnosis.get("objective") != objective:
            raise ResearchError("remedy diagnosis belongs to a different objective")
        selected = None
        selected_alpha = None
        selected_signatures = []
        for alpha_id, row in (diagnosis.get("alphas") or {}).items():
            candidate = next((item for item in row.get("remedy_candidates", [])
                              if item.get("remedy_id") == remedy_id), None)
            if candidate is not None:
                selected, selected_alpha = candidate, alpha_id
                signatures, facts = row.get("signatures", []), row.get("field_facts", {})
                selected_signatures = list(signatures)
                if not any(item.get("remedy_id") == remedy_id for item in
                           self.knowledge.lookup_remedies(signatures, facts)["verified"]):
                    selected = None
                    break
                break
        if selected is None:
            raise ResearchError("remedy is not a currently verified candidate for this diagnosis")
        mechanism_tags = diagnosis.get("mechanism_tags") or []
        dataset_id = diagnosis.get("dataset_id")
        rounds = 0
        for prereg_path in self.runs_root.rglob("prereg.json"):
            if (prereg_path.parent / "void.json").exists():
                continue
            try:
                prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            ref = prereg.get("remedy_reference") or {}
            if (prereg.get("dataset_id") == dataset_id and
                    sorted(ref.get("mechanism_tags") or []) == sorted(mechanism_tags) and
                    {key: (prereg.get("scope") or {}).get(key) for key in
                     ("region", "delay", "universe")} == (diagnosis.get("scope") or {})):
                rounds += 1
        if rounds >= 2 and not allow_round_overrun:
            raise ResearchError("remedy lineage exhausted at two rounds; explicit override required")
        return {"diagnosis_id": diagnosis_id, "diagnosis_ref": str(diagnosis_path),
                "source_batch": diagnosis.get("batch"), "source_alpha_id": selected_alpha,
                "remedy_id": remedy_id, "dataset_id": dataset_id,
                "scope": diagnosis.get("scope") or {},
                "mechanism_tags": mechanism_tags,
                "target_signatures": selected_signatures, "round": rounds + 1,
                "remedy": selected}

    def _read_case(self, objective, scope):
        path = self._case_path(objective, scope)
        case = (json.loads(path.read_text(encoding="utf-8")) if path.exists()
                else {"objective": objective,
                      "scope": {key: scope[key] for key in
                                ("region", "universe", "delay")},
                      "contenders": {}, "batches": []})
        if (case.get("objective") != objective or
                case.get("scope") != {key: scope[key] for key in
                                      ("region", "universe", "delay")}):
            raise ResearchError("case scope changed; research pooling blocked")
        return case

    def _scope(self, objective, scope_override=None):
        scope = dict(self.config.get("scope") or {})
        for key, value in (scope_override or {}).items():
            if scope.get(key) is not None and scope[key] != value:
                raise ResearchError(f"fixed case scope conflicts with config: {key}")
            scope[key] = value
        if not scope.get("region"):
            raise ResearchError("Region is required before platform research")
        options = self.gateway.call("get_platform_setting_options")
        combos = options.get("instrument_options") if isinstance(options, dict) else None
        if not isinstance(combos, list) or not combos:
            raise ResearchError("live platform setting options unavailable")
        effective = dict(scope)
        schema = self.gateway.call("get_simulation_settings_schema")
        if not isinstance(schema, dict):
            raise ResearchError("live simulation settings schema unavailable")
        selected_keys = ("universe", "delay", "neutralization", "decay",
                         "truncation", "pasteurization")
        if any(effective.get(key) is None for key in selected_keys):
            selected = self._decide("select_scope", {
                "objective": objective, "supplied_scope": scope,
                "supported_combinations": combos,
                "supported_settings_schema": {
                    key: schema.get(key) for key in
                    ("decay", "truncation", "pasteurization")}})
            if not isinstance(selected.get("reason"), str) or not selected["reason"].strip():
                raise ResearchError("effective setting selection needs an economic reason")
            effective["_settings_reason"] = selected["reason"]
            for key in selected_keys:
                if effective.get(key) is None:
                    effective[key] = selected.get(key)
        selected_row = next((row for row in combos if isinstance(row, dict)
                             and row.get("InstrumentType") == "EQUITY"
                             and row.get("Region") == effective["region"]
                             and row.get("Delay") == effective.get("delay")
                             and effective.get("universe") in (row.get("Universe") or [])
                             and effective.get("neutralization") in
                             (row.get("Neutralization") or [])), None)
        if selected_row is None:
            raise ResearchError("effective scope is not a live supported combination")
        effective["_supported_neutralizations"] = selected_row["Neutralization"]
        for key in ("decay", "truncation"):
            rule = schema.get(key)
            value = effective.get(key)
            expected = int if key == "decay" else (int, float)
            if (not isinstance(rule, dict) or not isinstance(value, expected)
                    or isinstance(value, bool)
                    or not isinstance(rule.get("minValue"), (int, float))
                    or not isinstance(rule.get("maxValue"), (int, float))
                    or not rule["minValue"] <= value <= rule["maxValue"]):
                raise ResearchError(f"{key} out of range for live simulation settings")
        for key, platform_key in (("pasteurization", "pasteurization"),
                                  ("unit_handling", "unitHandling"),
                                  ("nan_handling", "nanHandling"),
                                  ("max_trade", "maxTrade")):
            rule = schema.get(platform_key)
            choices = (rule.get("choices") if isinstance(rule, dict) else None)
            if (not isinstance(choices, list) or effective.get(key) not in
                    {row.get("value") for row in choices if isinstance(row, dict)}):
                raise ResearchError(f"{key} is not a live supported setting")
        for key in ("decay", "truncation", "pasteurization",
                    "unit_handling", "nan_handling", "test_period", "max_trade"):
            if effective.get(key) is None:
                raise ResearchError(f"missing effective scope setting: {key}")
        effective["_test_period_evidence"] = _validate_test_period(
            effective["test_period"], schema.get("testPeriod"))
        return effective

    def _platform_settings(self, scope, *, probe=False):
        return {
            "instrument_type": "EQUITY",
            "region": scope["region"], "universe": scope["universe"],
            "delay": scope["delay"], "decay": 0 if probe else scope["decay"],
            "neutralization": "NONE" if probe else scope["neutralization"],
            "truncation": scope["truncation"],
            "pasteurization": scope["pasteurization"],
            "unit_handling": scope["unit_handling"],
            "nan_handling": scope["nan_handling"],
            "language": "FASTEXPR", "visualization": False,
            "test_period": scope["test_period"],
            "max_trade": scope["max_trade"],
        }

    def _ensure_expressions_uncached(self, expressions, dataset_id, scope, settings,
                                     batch_dir, *, label):
        """Reject repeated expressions and Bloom possible-hits before simulation."""
        seen = set()
        problems = []
        try:
            for expression in expressions:
                fingerprint = self.backtest_cache.fingerprint(
                    expression, dataset_id, scope, settings)
                if fingerprint in seen:
                    problems.append({"expression": expression,
                                     "reason": "duplicate in current batch"})
                    continue
                seen.add(fingerprint)
                if self.backtest_cache.find(expression, dataset_id, scope, settings):
                    problems.append({"expression": expression,
                                     "reason": "Bloom maybe-hit; false positive possible"})
        except BacktestCacheError as exc:
            reason = f"backtest cache lookup failed before {label}: {exc}"
            try:
                self.void_batch(batch_dir, reason)
            except ResearchError as void_error:
                reason += f"; draft needs recovery: {void_error}"
            raise ResearchError(reason) from exc
        if not problems:
            return
        summary = "; ".join(f"{item['expression']} ({item['reason']})"
                             for item in problems)
        reason = f"backtest cache hit before {label}: {summary}"
        try:
            self.void_batch(batch_dir, reason)
        except ResearchError as exc:
            raise ResearchError(f"{reason}; draft requires recovery: {exc}") from exc
        raise ResearchError(f"{reason}; draft marked VOID")

    def _record_completed_expressions(self, expressions, dataset_id, scope, settings):
        records = [{"expression": expression, "dataset_id": dataset_id,
                    "scope": scope, "settings": settings}
                   for expression in expressions]
        try:
            self.backtest_cache.record_many(records)
        except BacktestCacheError as exc:
            raise ResearchError(f"completed backtest could not be cached: {exc}") from exc

    def _record_probe_batch(self, context, expressions, rows, settings):
        alpha_ids = [row.get("alpha_id") for row in rows if isinstance(row, dict)
                     and isinstance(row.get("alpha_id"), str) and row["alpha_id"]]
        if len(alpha_ids) != len(expressions) or len(set(alpha_ids)) != len(alpha_ids):
            raise ResearchError("diagnostic children unresolved; do not mark READ_BACK")
        self._record_completed_expressions(
            expressions, context["dataset_id"], context["scope"], settings)

    @staticmethod
    def _has_complete_runner_research(research):
        required = ("idea", "data_rationale", "operator_rationale",
                    "field_relationship", "settings_rationale",
                    "expected_behavior", "failure_criterion")
        return (isinstance(research, dict) and all(
            isinstance(research.get(key), str) and research[key].strip()
            for key in required))

    @staticmethod
    def _production_runner_variant(proposal, variant, dataset_id, index):
        return {
            "name": f"variant_{index}", "expression": variant["expression"], "dataset": dataset_id,
            "research": {
                "idea": proposal["hypothesis"].strip() + " Variant rationale: " + variant["reason"].strip(),
                "data_rationale": proposal["data_rationale"].strip(),
                "operator_rationale": proposal["operator_rationale"].strip(),
                "field_relationship": proposal["field_relationship"].strip(),
                "settings_rationale": proposal["settings_rationale"].strip(),
                "expected_behavior": proposal["expected_sign"].strip(),
                "failure_criterion": proposal["failure_criterion"].strip(),
            },
        }

    @staticmethod
    def _normalize_runner_result(result):
        rows = result.get("results") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise ResearchError("platform simulation returned no result rows")
        normalized = []
        for row in rows:
            if not isinstance(row, dict):
                normalized.append(row)
                continue
            item = dict(row)
            if not isinstance(item.get("details"), dict):
                item["details"] = {"is": item.get("is")}
            normalized.append(item)
        return {"alpha_results": normalized,
                "runner": result.get("runner") if isinstance(result, dict) else None}

    def _simulate(self, variants, settings, dataset_id, batch_dir, label):
        intent_path = Path(batch_dir) / ("simulation_intent.json" if label == "production"
                                         else f"{label}_intent.json")
        intent = (json.loads(intent_path.read_text(encoding="utf-8"))
                  if intent_path.exists() else None)
        if self.stop_flag and self.stop_flag.exists():
            if isinstance(intent, dict) and intent.get("status") == "PREPARED":
                intent["status"] = "STOPPED_BEFORE_SUBMISSION"
                atomic_write_json(intent_path, intent)
            raise ResearchError("stop flag is set; no new simulation posted")
        for variant in variants:
            if not self._has_complete_runner_research(variant.get("research")):
                raise ResearchError("canonical simulation requires complete per-variant research evidence")
        if isinstance(intent, dict):
            if intent.get("status") not in {"PREPARED", "STOPPED_BEFORE_SUBMISSION"}:
                raise ResearchError("simulation intent is not proven unsubmitted")
            intent["status"] = "SUBMITTING"
            atomic_write_json(intent_path, intent)
        result = self.gateway.call(
            "simulate_batch", variants=variants, settings=settings,
            dataset_id=dataset_id, batch_id=Path(batch_dir).name, label=label)
        return self._normalize_runner_result(result)

    def _unfinished_batch(self):
        if not self.runs_root.exists():
            return None
        for path in self.runs_root.rglob("batch_context.json"):
            batch_dir = path.parent
            if (batch_dir / "void.json").exists():
                continue
            intent_path = batch_dir / "simulation_intent.json"
            results_path = batch_dir / "results.json"
            if not results_path.exists():
                return batch_dir
            if intent_path.exists():
                try:
                    intent = json.loads(intent_path.read_text(encoding="utf-8"))
                    closed = intent.get("status") == "READ_BACK"
                except (OSError, ValueError):
                    closed = False
                if not closed:
                    return batch_dir
                prereg_path = batch_dir / "prereg.json"
                if prereg_path.exists():
                    try:
                        prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
                        if prereg.get("remedy_reference"):
                            diagnosis = json.loads((batch_dir / "diagnosis.json").read_text(
                                encoding="utf-8"))
                            if (diagnosis.get("remedy_outcome") or {}).get("status") != "recorded":
                                return batch_dir
                    except (OSError, ValueError):
                        return batch_dir
            elif (batch_dir / "prereg.json").exists():
                return path.parent
        for batch_dir in self.runs_root.glob("*/*/*/*/*"):
            if (batch_dir.is_dir() and
                    re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{8}", batch_dir.name) and
                    not (batch_dir / "results.json").exists() and
                    not (batch_dir / "void.json").exists()):
                return batch_dir
        return None

    def void_batch(self, batch_dir: Path, reason: str) -> dict:
        batch_dir = Path(batch_dir).resolve()
        if not batch_dir.is_relative_to(self.runs_root.resolve()):
            raise ResearchError("batch path outside research runs")
        if not reason or not reason.strip():
            raise ResearchError("void reason is required")
        context_path = batch_dir / "batch_context.json"
        if not context_path.exists():
            # A process can stop after mkdir and before the first durable batch
            # artifact. Only this provably unsubmitted empty directory is voidable.
            relative_parts = batch_dir.relative_to(self.runs_root.resolve()).parts
            if (not batch_dir.is_dir() or len(relative_parts) != 5 or
                    not re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{8}", batch_dir.name)):
                raise ResearchError("contextless batch is not a recognized batch directory")
            if any((batch_dir / name).exists() for name in
                   ("prereg.json", "simulation_intent.json", "simulation_raw.json",
                    "state.json", "gate.json", "results.json")) or list(
                       batch_dir.glob("probe_*_intent.json")) or list(
                       batch_dir.glob("probe_*_raw.json")):
                raise ResearchError("contextless batch has submission evidence; recovery required")
            record = {"status": "VOID", "reason": reason.strip(),
                      "at": datetime.now().astimezone().isoformat()}
            atomic_write_json(batch_dir / "void.json", record)
            return record
        probe_intents = list(batch_dir.glob("probe_*_intent.json"))
        if ((batch_dir / "simulation_intent.json").exists() or any(
                json.loads(path.read_text(encoding="utf-8")).get("status") != "READ_BACK"
                or not (batch_dir / path.name.replace("_intent.json", "_raw.json")).exists()
                for path in probe_intents)):
            raise ResearchError("submitted or possibly submitted batch requires recovery")
        if (batch_dir / "results.json").exists():
            raise ResearchError("completed batch cannot be voided")
        record = {"status": "VOID", "reason": reason.strip(),
                  "at": datetime.now().astimezone().isoformat()}
        atomic_write_json(batch_dir / "void.json", record)
        context = json.loads(context_path.read_text(encoding="utf-8"))
        if not isinstance(context, dict) or not isinstance(context.get("scope"), dict) or not context.get("objective"):
            return record
        case_path = self._case_path(context["objective"], context["scope"])
        campaign = self.campaigns.get(case_path.stem)
        if campaign.get("current_batch") == str(batch_dir):
            self.campaigns.update(case_path.stem, current_batch=None,
                                  dataset_state="draft_voided", status="draft_voided",
                                  candidate_ledger_ref=str(case_path))
        return record

    def _apply_canonical_audit(self, batch_dir, prereg, alpha_ids, readback, gate,
                               contender_ids=None, *, audit_id=None, error_dir=None):
        raw_path = Path(batch_dir) / "simulation_raw.json"
        try:
            runner = json.loads(raw_path.read_text(encoding="utf-8")).get("runner")
            if not isinstance(runner, dict):
                raise ResearchError("canonical runner evidence is missing")
            results_path = Path(runner["output"]).resolve()
            receipt_path = results_path.with_suffix(".admission.json")
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            variants = receipt.get("variants")
            if not isinstance(variants, list):
                raise ResearchError("canonical admission receipt has no variants")
            entries = {}
            for index, alpha_id in enumerate(alpha_ids):
                expression = prereg["variants"][index]["expression"]
                matches = [row for row in variants
                           if isinstance(row, dict) and row.get("expression") == expression]
                if len(matches) != 1:
                    raise ResearchError("canonical receipt variant does not bind uniquely")
                review = readback[alpha_id].get("review") or {}
                review_reason = review.get("reason")
                if not isinstance(review_reason, str) or not review_reason.strip():
                    review_reason = "No completed model review evidence was recorded."
                review_clear = (review.get("verdict") == "CLEAR"
                                and isinstance(review.get("evidence_refs"), list)
                                and bool(review["evidence_refs"]))
                review_status = "clear" if review_clear else "pending"
                reviews = {
                    "robustness": {"status": review_status, "evidence": review_reason},
                    "tradeoff": {"status": review_status, "evidence": review_reason},
                }
                entries[alpha_id] = {
                    "dataset": prereg["dataset_id"], "subtype": "REGULAR",
                    "receipt": str(receipt_path), "results": str(results_path),
                    "variant": matches[0].get("name"),
                    "field_cache_root": runner.get("field_cache_root"),
                    "reviews": reviews,
                }
            params = {"case_key": runner["case_key"], "run_id": runner["run_id"],
                      "batch_id": Path(batch_dir).name, "alpha_ids": alpha_ids, "entries": entries,
                      "self_corr_max": self.config["gate"]["self_corr_max"],
                      "contender_ids": contender_ids if contender_ids is not None else alpha_ids}
            if audit_id is not None:
                params["audit_id"] = audit_id
            audited = self.gateway.call("audit_alphas", **params)
            rows = audited.get("results") if isinstance(audited, dict) else None
            audit_path = audited.get("audit_path") if isinstance(audited, dict) else None
            if not isinstance(rows, list) or any(
                    not isinstance(row, dict) or not isinstance(row.get("alpha_id"), str)
                    or not row["alpha_id"] for row in rows):
                raise ResearchError("canonical audit rows are malformed")
            by_id, duplicates = {}, set()
            for row in rows:
                alpha_id = row["alpha_id"]
                if alpha_id in by_id:
                    duplicates.add(alpha_id)
                else:
                    by_id[alpha_id] = row
            for alpha_id in duplicates:
                by_id.pop(alpha_id, None)
        except Exception as exc:
            error_path = (Path(error_dir) / f"{audit_id}_error.json"
                          if error_dir is not None and audit_id is not None
                          else Path(batch_dir) / "canonical_audit_error.json")
            atomic_write_json(error_path, {
                "status": "UNAVAILABLE", "error_type": type(exc).__name__,
                "reason": str(exc)[:2000],
                "at": datetime.now().astimezone().isoformat()})
            by_id = {}
            audit_path = None

        for alpha_id, verdict in gate.items():
            result = by_id.get(alpha_id)
            if not isinstance(result, dict):
                verdict.setdefault("pending", []).append("canonical_audit_unavailable")
                verdict["pending"] = sorted(set(verdict["pending"]))
                if verdict.get("status") == "CANDIDATE":
                    verdict["status"] = "PENDING_FULL_AUDIT"
                verdict["canonical_audit"] = {"status": "unavailable",
                                               "audit_path": audit_path}
                continue
            audit_summary = {
                "candidate_eligible": result.get("candidate_eligible"),
                "evidence_complete": result.get("evidence_complete"),
                "candidate_status": result.get("candidate_status"),
                "risk_neutralized_state": result.get("risk_neutralized_state"),
                "gate_failures": result.get("gate_failures", []),
                "gate_pending": result.get("gate_pending", []),
                "audit_path": audit_path,
            }
            configured_gate = self.config.get("gate")
            effective_gate = dict(configured_gate) if isinstance(configured_gate, dict) else {}
            for key in ("risk_neutralized_sharpe_min", "risk_neutralized_fitness_min"):
                effective_gate.setdefault(key, DEFAULT_CONFIG["gate"][key])
            audit_summary["gate_config"] = effective_gate
            verdict["canonical_audit"] = audit_summary
            try:
                expression = prereg["variants"][alpha_ids.index(alpha_id)]["expression"]
                reconciled = reconcile_audit_evidence(
                    alpha_id, result, verdict, readback.get(alpha_id) or {},
                    prereg, self.config, expression)
            except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
                reconciled = {"failures": [], "pending": [
                    f"canonical_audit:reconciliation_unavailable:{type(exc).__name__}"]}
            audit_summary["local_gate"] = reconciled.get("local_gate")
            audit_summary["local_correlations"] = reconciled.get("correlations")
            if (isinstance(reconciled.get("local_gate"), dict)
                    and "ppa_eligibility" in reconciled["local_gate"]):
                verdict["ppa_eligibility"] = reconciled["local_gate"]["ppa_eligibility"]
            verdict["failures"] = sorted(set(verdict.get("failures", []) +
                                              reconciled.get("failures", [])))
            verdict["pending"] = sorted(set(verdict.get("pending", []) +
                                             reconciled.get("pending", [])))
            if any(item.startswith("pairwise:") for item in verdict["failures"]):
                verdict["status"] = "PAIRWISE_CONFLICT"
            elif any(item.startswith("ra:") for item in verdict["failures"]):
                verdict["status"] = "RA_FAILED"
            elif any(item.startswith("platform_check:") for item in verdict["failures"]):
                verdict["status"] = "PLATFORM_CHECK_FAILED"
            elif any(item.endswith("_correlation") for item in verdict["failures"]):
                verdict["status"] = "CORR_REJECTED"
            elif verdict["failures"] and verdict.get("status") == "CANDIDATE":
                verdict["status"] = "METRICS_FAILED"
            elif verdict["pending"] and verdict.get("status") == "CANDIDATE":
                verdict["status"] = "PENDING_FULL_AUDIT"
        return by_id

    def _refresh_retained_contenders(self, contenders, current_ids, scope, self_limit, batch_dir):
        """Recheck retained candidates before using them in the active review pool."""
        evidence, unresolved = {}, []
        for alpha_id, item in contenders.items():
            if alpha_id in current_ids or item.get("local_self_rejected"):
                continue
            details = None
            try:
                details = self.gateway.call("get_alpha_details", alpha_id=alpha_id)
                settings = details.get("settings") if isinstance(details, dict) else None
                if (not isinstance(details, dict) or details.get("id") != alpha_id
                        or details.get("type") != "REGULAR" or not isinstance(settings, dict)
                        or settings.get("instrumentType") != "EQUITY"
                        or any(settings.get(key) != scope.get(key)
                               for key in ("region", "universe", "delay"))
                        or type(settings.get("delay")) is not int):
                    raise ResearchError("retained contender scope is unavailable or changed")
                is_data = details.get("is")
                checks = {"checks": is_data.get("checks") if isinstance(is_data, dict) else None}
                verdict = evaluate_candidate(details, checks, scope["region"], self.config)
                entry = {"details": details, "ra_fail_count": verdict["ra_fail_count"]}
                item["retained_unverified"] = False
                if verdict["ra_fail_count"]:
                    item["ra_rejected"] = True
                    self._register_alpha_state(alpha_id, {"state": "RA_FAILED",
                        "ra_fail_count": verdict["ra_fail_count"], "ra_readable": True,
                        "self_corr_max": self_limit})
                    if item.get("status") != "PAIRWISE_CONFLICT":
                        item["status"] = "RA_FAILED"
                    entry["status"] = "ra_failed"
                elif not verdict["correlation_allowed"]:
                    raise ResearchError("retained contender RA checks are unavailable")
                else:
                    if item.get("ra_rejected") or item.get("status") == "RA_FAILED":
                        item["status"] = "EVIDENCE_PENDING"
                    item["ra_rejected"] = False
                    state = {"state": "RA_PASSED", "ra_fail_count": 0,
                             "ra_readable": True, "self_corr_max": self_limit}
                    self._register_alpha_state(alpha_id, state)
                    try:
                        local_self = self.gateway.call("check_local_self_correlation",
                                                       alpha_id=alpha_id, self_corr_max=self_limit,
                                                       batch_id=Path(batch_dir).name)
                    except Exception as exc:
                        local_self = {"status": "pending", "reason": type(exc).__name__,
                                      "limit": self_limit}
                    latest = local_self.get("ra_details") if isinstance(local_self, dict) else None
                    if isinstance(latest, dict) and latest.get("id") == alpha_id:
                        details = latest
                        is_data = details.get("is")
                        checks = {"checks": is_data.get("checks") if isinstance(is_data, dict) else None}
                        verdict = evaluate_candidate(details, checks, scope["region"], self.config)
                        entry["prefilter_details"] = details
                        entry["ra_fail_count"] = verdict["ra_fail_count"]
                        if verdict["ra_fail_count"]:
                            item["ra_rejected"] = True
                            if item.get("status") != "PAIRWISE_CONFLICT":
                                item["status"] = "RA_FAILED"
                            self._register_alpha_state(alpha_id, {"state": "RA_FAILED",
                                "ra_fail_count": verdict["ra_fail_count"], "ra_readable": True,
                                "self_corr_max": self_limit})
                            entry.update({"status": "ra_failed", "local_self": local_self})
                            evidence[alpha_id] = entry
                            continue
                        if not verdict["correlation_allowed"]:
                            raise ResearchError("retained contender RA checks changed or are unavailable")
                    state["local_self"] = local_self
                    self._register_alpha_state(alpha_id, state)
                    local_state = local_self_correlation_state(local_self, self_limit)
                    item["local_self_rejected"] = local_state == "fail"
                    if local_state == "fail":
                        item["status"] = "CORR_REJECTED"
                    elif (local_state == "pending" or any(
                            value.startswith("ra_pending:") for value in verdict["pending"])):
                        if item.get("status") == "CANDIDATE":
                            item["status"] = "EVIDENCE_PENDING"
                    entry.update({"status": local_state, "local_self": local_self})
                evidence[alpha_id] = entry
            except Exception as exc:
                item["retained_unverified"] = True
                self._register_alpha_state(alpha_id, {"state": "RA_UNAVAILABLE",
                    "ra_fail_count": None, "ra_readable": False, "self_corr_max": self_limit})
                if item.get("status") == "CANDIDATE":
                    item["status"] = "EVIDENCE_PENDING"
                evidence[alpha_id] = {"status": "unavailable", "error_type": type(exc).__name__,
                                      "details": details}
                unresolved.append(alpha_id)
        if evidence:
            atomic_write_json(Path(batch_dir) / "retained_correlation_precheck.json", evidence)
        return unresolved, evidence

    def _reaudit_retained_candidates(self, contenders, current_ids, prechecks, scope,
                                     batch_dir, unresolved):
        """Re-establish every retained Candidate from current and bound evidence."""
        candidates = [alpha_id for alpha_id, item in contenders.items()
                      if alpha_id not in current_ids and item.get("reaudit_required")
                      and item.get("status") != "PAIRWISE_CONFLICT"
                      and not item.get("local_self_rejected") and not item.get("ra_rejected")
                      and not item.get("retained_unverified")]
        if not candidates:
            return
        context = json.loads((Path(batch_dir) / "batch_context.json").read_text(encoding="utf-8"))
        current_case = {"case_key": context.get("platform_case_key"),
                        "run_id": context.get("platform_run_id")}
        if not all(isinstance(value, str) and value for value in current_case.values()):
            raise ResearchError("current case runner is unavailable for retained re-audit")
        reaudits = {}
        try:
            for alpha_id in candidates:
                item = contenders[alpha_id]
                if item.get("status") == "PAIRWISE_CONFLICT":
                    continue
                verdict = None
                try:
                    prior_dir = Path(item["batch"]).resolve()
                    if not prior_dir.is_relative_to(self.runs_root.resolve()):
                        raise ResearchError("retained batch is outside this research root")
                    prereg_path = prior_dir / "prereg.json"
                    state = json.loads((prior_dir / "state.json").read_text(encoding="utf-8"))
                    if not verify_prereg(prereg_path, state):
                        raise ResearchError("retained pre-registration changed")
                    prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
                    prior = json.loads((prior_dir / "results.json").read_text(encoding="utf-8"))
                    intent = json.loads((prior_dir / "simulation_intent.json").read_text(encoding="utf-8"))
                    alpha_ids = prior.get("alpha_ids")
                    variants = prereg.get("variants")
                    if (intent.get("status") != "READ_BACK" or not isinstance(alpha_ids, list)
                            or len(alpha_ids) != len(set(alpha_ids)) or alpha_id not in alpha_ids
                            or not isinstance(variants, list) or len(variants) != len(alpha_ids)):
                        raise ResearchError("retained batch result does not bind the Alpha")
                    variant = variants[alpha_ids.index(alpha_id)]
                    expression = variant.get("expression") if isinstance(variant, dict) else None
                    prior_readback = (prior.get("readback") or {}).get(alpha_id)
                    prior_details = prior_readback.get("details") if isinstance(prior_readback, dict) else None
                    prior_code = ((prior_details.get("regular") or {}).get("code")
                                  if isinstance(prior_details, dict) else None)
                    if not isinstance(expression, str) or prior_code != expression:
                        raise ResearchError("retained expression is not bound to its pre-registration")
                    prereg = {**prereg, "variants": [variant]}
                    self.gateway.call("set_case", **current_case)
                    self_limit = self.config["gate"]["self_corr_max"]
                    state = {"state": "RA_PASSED", "ra_fail_count": 0,
                             "ra_readable": True, "self_corr_max": self_limit}
                    self._register_alpha_state(alpha_id, state)
                    local_self = self.gateway.call("check_local_self_correlation",
                                                   alpha_id=alpha_id, self_corr_max=self_limit,
                                                   batch_id=Path(batch_dir).name)
                    state["local_self"] = local_self
                    self._register_alpha_state(alpha_id, state)
                    detail = ((local_self.get("ra_details") if isinstance(local_self, dict) else None)
                              or (prechecks.get(alpha_id) or {}).get("prefilter_details")
                              or (prechecks.get(alpha_id) or {}).get("details"))
                    if not isinstance(detail, dict) or detail.get("id") != alpha_id:
                        raise ResearchError("retained live read-back is unavailable")
                    checks = {"checks": (detail.get("is") or {}).get("checks")}
                    other_ids = [other for other, row in contenders.items()
                                 if other != alpha_id and other not in current_ids
                                 and not row.get("local_self_rejected") and not row.get("ra_rejected")
                                 and not row.get("retained_unverified")]
                    raw, correlations = None, None
                    verdict = evaluate_candidate(
                        detail, checks, scope["region"], self.config,
                        expected_pairwise_ids=other_ids,
                        operator_count=(prereg.get("operator_counts") or {}).get(expression),
                        local_self=local_self, **ppa_gate_kwargs(prereg, expression))
                    if local_self_correlation_state(local_self, self_limit) == "pass":
                        raw = self.gateway.call("check_correlation", alpha_id=alpha_id,
                                                contender_ids=other_ids, self_corr_max=self_limit,
                                                batch_id=Path(batch_dir).name)
                        correlations = parse_correlations(raw, other_ids)
                    verdict = evaluate_candidate(
                        detail, checks, scope["region"], self.config, correlations=correlations,
                        pairwise=(correlations or {}).get("pairwise"), expected_pairwise_ids=other_ids,
                        operator_count=(prereg.get("operator_counts") or {}).get(expression),
                        local_self=local_self, **ppa_gate_kwargs(prereg, expression))
                    verdict["correlations"] = correlations
                    verdict["local_self_correlation"] = local_self
                    readback = {"details": detail, "checks": checks,
                                "local_self_correlation": local_self, "correlation_raw": raw}
                    if unresolved:
                        verdict["pending"].extend("retained_contender_unverified:" + other
                                                  for other in unresolved if other != alpha_id)
                        if verdict["pending"] and verdict["status"] == "CANDIDATE":
                            verdict["status"] = "EVIDENCE_PENDING"
                    if verdict["status"] == "CANDIDATE":
                        review = self._decide("review_candidate", {
                            "objective": prereg["objective"], "dataset_id": prereg["dataset_id"],
                            "scope": prereg["scope"], "preregistration": prereg,
                            "alpha_id": alpha_id, "evidence": readback, "gate": verdict},
                            batch_dir=batch_dir)
                        readback["review"] = review
                        if (review.get("verdict") != "CLEAR" or not isinstance(review.get("reason"), str)
                                or not review["reason"].strip() or not isinstance(review.get("evidence_refs"), list)
                                or not review["evidence_refs"] or any(ref not in {
                                    "preregistration", "readback", "correlation", "gate", "probe_evidence"}
                                    for ref in review["evidence_refs"])):
                            verdict["status"] = "REVIEW_PENDING"
                            verdict["pending"].append("candidate_review")
                    gate = {alpha_id: verdict}
                    self._apply_canonical_audit(
                        prior_dir, prereg, [alpha_id], {alpha_id: readback}, gate,
                        contender_ids=[alpha_id, *other_ids],
                        audit_id=f"{Path(batch_dir).name}_{alpha_id}", error_dir=batch_dir)
                    verdict = gate[alpha_id]
                    if local_self_correlation_state(local_self, self_limit) == "fail":
                        item["local_self_rejected"] = True
                    if verdict.get("ra_fail_count"):
                        item["ra_rejected"] = True
                except Exception as exc:
                    if not isinstance(verdict, dict):
                        verdict = {"status": "PENDING_FULL_AUDIT", "failures": [], "pending": []}
                    verdict.setdefault("pending", []).append("retained_reaudit_unavailable:" + type(exc).__name__)
                    if verdict.get("status") == "CANDIDATE":
                        verdict["status"] = "PENDING_FULL_AUDIT"
                item["status"] = verdict["status"]
                item["reaudit_required"] = verdict["status"] != "CANDIDATE"
                reaudits[alpha_id] = verdict
                for failure in verdict.get("failures", []):
                    if failure.startswith("pairwise:"):
                        other = failure.split(":", 1)[1]
                        if other in contenders:
                            contenders[other]["status"] = "PAIRWISE_CONFLICT"
                            contenders[other]["reaudit_required"] = True
                            reaudits[other] = {"status": "PAIRWISE_CONFLICT",
                                "failures": ["pairwise:" + alpha_id],
                                "pending": ["pairwise_conflict_requires_user_choice"]}
                for pending in verdict.get("pending", []):
                    if pending.startswith("pairwise_unavailable:"):
                        other = pending.split(":", 1)[1]
                        if other in contenders and contenders[other].get("status") == "CANDIDATE":
                            contenders[other]["status"] = "PAIRWISE_PENDING"
                            contenders[other]["reaudit_required"] = True
            atomic_write_json(Path(batch_dir) / "retained_candidate_audits.json", reaudits)
        finally:
            self.gateway.call("set_case", **current_case)

    def _complete(self, batch_dir, prereg, alpha_ids, digest,
                  simulation_settings=None):
        readback = {}
        for alpha_id in alpha_ids:
            details = self.gateway.call("get_alpha_details", alpha_id=alpha_id)
            if not isinstance(details, dict) or details.get("id") != alpha_id:
                raise ResearchError("Alpha read-back ID mismatch; gate remains pending")
            is_data = details.get("is") if isinstance(details, dict) else None
            readback[alpha_id] = {"details": details,
                                  "checks": {"checks": (
                                      is_data.get("checks") if isinstance(is_data, dict)
                                      else None)}}
        scope = prereg["scope"]
        simulation_settings = (simulation_settings or prereg.get("simulation_settings")
                               or self._platform_settings(scope))
        case_path = self._case_path(prereg["objective"], scope)
        case = self._read_case(prereg["objective"], scope)
        operator_counts = prereg.get("operator_counts") or {}
        prelim = {}
        for alpha_id, evidence in readback.items():
            expression = prereg["variants"][alpha_ids.index(alpha_id)]["expression"]
            prelim[alpha_id] = evaluate_candidate(
                evidence["details"], evidence["checks"], scope["region"],
                self.config, operator_count=operator_counts.get(expression),
                **ppa_gate_kwargs(prereg, expression))
        contenders = dict(case.get("contenders") or {})
        for alpha_id, item in contenders.items():
            if alpha_id not in alpha_ids and item.get("status") == "CANDIDATE":
                item["status"] = "PENDING_FULL_AUDIT"
                item["reaudit_required"] = True
        case["contenders"] = contenders
        atomic_write_json(case_path, case)
        self_limit = self.config["gate"]["self_corr_max"]
        retained_unverified, retained_prechecks = self._refresh_retained_contenders(
            contenders, alpha_ids, scope, self_limit, batch_dir)
        self._reaudit_retained_candidates(contenders, alpha_ids, retained_prechecks, scope,
                                          batch_dir, retained_unverified)
        for alpha_id in alpha_ids:
            verdict = prelim[alpha_id]
            if verdict["correlation_allowed"]:
                contenders.setdefault(alpha_id, {"status": "EVIDENCE_PENDING",
                                                 "batch": str(batch_dir)})
        prefilters = {}
        new_unverified = []
        for alpha_id, evidence in readback.items():
            if not prelim[alpha_id]["correlation_allowed"]:
                continue
            state = {"state": "RA_PASSED", "ra_fail_count": 0,
                     "ra_readable": True, "self_corr_max": self_limit}
            self._register_alpha_state(alpha_id, state)
            try:
                prefilter = self.gateway.call("check_local_self_correlation",
                                              alpha_id=alpha_id, self_corr_max=self_limit,
                                              batch_id=Path(batch_dir).name)
            except Exception as exc:
                prefilter = {"status": "pending", "reason": type(exc).__name__,
                             "limit": self_limit}
            prefilters[alpha_id] = prefilter
            evidence["local_self_correlation"] = prefilter
            latest = prefilter.get("ra_details") if isinstance(prefilter, dict) else None
            if isinstance(latest, dict) and latest.get("id") == alpha_id:
                evidence["previous_details"] = evidence["details"]
                evidence["details"] = latest
                latest_is = latest.get("is")
                evidence["checks"] = {"checks": latest_is.get("checks")
                                      if isinstance(latest_is, dict) else None}
                expression = prereg["variants"][alpha_ids.index(alpha_id)]["expression"]
                prelim[alpha_id] = evaluate_candidate(
                    latest, evidence["checks"], scope["region"], self.config,
                    operator_count=operator_counts.get(expression),
                    **ppa_gate_kwargs(prereg, expression))
            current = prelim[alpha_id]
            if not current["correlation_allowed"]:
                failed = current["ra_fail_count"]
                state = {"state": "RA_FAILED" if failed else "RA_UNAVAILABLE",
                         "ra_fail_count": failed if failed else None,
                         "ra_readable": bool(failed), "self_corr_max": self_limit}
                if alpha_id in contenders:
                    contenders[alpha_id]["ra_rejected"] = bool(failed)
                    contenders[alpha_id]["retained_unverified"] = not bool(failed)
                    if not failed:
                        new_unverified.append(alpha_id)
            else:
                state["local_self"] = prefilter
            self._register_alpha_state(alpha_id, state)
            if alpha_id in contenders:
                contenders[alpha_id]["local_self_rejected"] = (
                    local_self_correlation_state(prefilter, self_limit) == "fail")
        gate = {}
        for alpha_id, evidence in readback.items():
            preliminary = prelim[alpha_id]
            correlations = None
            expected = [other for other, item in contenders.items()
                        if other != alpha_id and not item.get("local_self_rejected")
                        and not item.get("ra_rejected") and not item.get("retained_unverified")]
            prefilter = prefilters.get(alpha_id)
            if (preliminary["correlation_allowed"] and
                    local_self_correlation_state(prefilter, self_limit) == "pass"):
                raw_correlation = self.gateway.call("check_correlation",
                                                    alpha_id=alpha_id,
                                                    contender_ids=expected,
                                                    self_corr_max=self_limit,
                                                    batch_id=Path(batch_dir).name)
                correlations = parse_correlations(raw_correlation, expected)
                evidence["correlation_raw"] = raw_correlation
            expression = prereg["variants"][alpha_ids.index(alpha_id)]["expression"]
            verdict = evaluate_candidate(
                evidence["details"], evidence["checks"],
                scope["region"], self.config,
                correlations=correlations,
                pairwise=(correlations or {}).get("pairwise"),
                expected_pairwise_ids=expected,
                operator_count=operator_counts.get(expression),
                **ppa_gate_kwargs(prereg, expression),
                local_self=((prefilter if isinstance(prefilter, dict) else {})
                            if preliminary["correlation_allowed"] else None))
            verdict["correlations"] = correlations
            verdict["local_self_correlation"] = prefilter
            verdict["pending"].extend("retained_contender_unverified:" + other
                                      for other in retained_unverified)
            verdict["pending"].extend("case_contender_unverified:" + other
                                      for other in new_unverified if other != alpha_id)
            if (retained_unverified or any(other != alpha_id for other in new_unverified)) and verdict["status"] == "CANDIDATE":
                verdict["status"] = "EVIDENCE_PENDING"
            if verdict["status"] == "CANDIDATE":
                review = self._decide("review_candidate", {
                    "objective": prereg["objective"],
                    "dataset_id": prereg["dataset_id"], "scope": scope,
                    "preregistration": prereg,
                    "alpha_id": alpha_id, "evidence": evidence,
                    "gate": verdict}, batch_dir=batch_dir)
                evidence["review"] = review
                if (review.get("verdict") != "CLEAR" or
                        not isinstance(review.get("reason"), str) or
                        not review["reason"].strip() or
                        not isinstance(review.get("evidence_refs"), list) or
                        not review["evidence_refs"] or
                        any(ref not in {"preregistration", "readback", "correlation",
                                        "gate", "probe_evidence"}
                            for ref in review["evidence_refs"])):
                    verdict["status"] = (review.get("verdict")
                                         if review.get("verdict") in {
                                             "ROBUSTNESS_REVIEW", "TRADEOFF_REVIEW"}
                                         else "REVIEW_PENDING")
                    verdict["pending"].append("candidate_review")
            gate[alpha_id] = verdict
            if alpha_id in contenders:
                contenders[alpha_id]["status"] = verdict["status"]
            for failed in verdict["failures"]:
                if failed.startswith("pairwise:"):
                    other = failed.split(":", 1)[1]
                    if other in contenders:
                        contenders[other]["status"] = "PAIRWISE_CONFLICT"
            for item in verdict["pending"]:
                if item.startswith("pairwise_unavailable:"):
                    other = item.split(":", 1)[1]
                    if other in contenders and contenders[other]["status"] == "CANDIDATE":
                        contenders[other]["status"] = "PAIRWISE_PENDING"
        for alpha_id, verdict in gate.items():
            current = contenders.get(alpha_id, {}).get("status")
            if current in {"PAIRWISE_CONFLICT", "PAIRWISE_PENDING"}:
                verdict["status"] = current
        self._apply_canonical_audit(batch_dir, prereg, alpha_ids, readback, gate,
                                    contender_ids=[alpha_id for alpha_id, item in contenders.items()
                                                   if not item.get("local_self_rejected")
                                                   and not item.get("ra_rejected")
                                                   and not item.get("retained_unverified")])
        for alpha_id, verdict in gate.items():
            if alpha_id in contenders:
                contenders[alpha_id]["status"] = verdict["status"]
        for alpha_id, verdict in gate.items():
            for failed in verdict["failures"]:
                if failed.startswith("pairwise:"):
                    other = failed.split(":", 1)[1]
                    if other in contenders:
                        contenders[other]["status"] = "PAIRWISE_CONFLICT"
        for alpha_id, verdict in gate.items():
            if contenders.get(alpha_id, {}).get("status") == "PAIRWISE_CONFLICT":
                verdict["status"] = "PAIRWISE_CONFLICT"
        case["contenders"] = contenders
        atomic_write_json(batch_dir / "gate.json", gate)
        proposal = prereg["proposal"]
        try:
            diagnosis = diagnose_batch(
                batch_dir, prereg, gate, readback, self.config,
                self.knowledge.lookup_remedies)
        except DiagnosisError as exc:
            raise ResearchError(f"diagnosis refused: {exc}") from exc
        failed_alphas = {alpha_id: row for alpha_id, row in
                         diagnosis["alphas"].items() if row["signatures"]}
        if not failed_alphas:
            diagnosis["model_diagnosis"] = {"status": "no_failure_signatures"}
        else:
            try:
                model_diagnosis = self._decide("diagnose_failures", {
                    "objective": prereg["objective"],
                    "dataset_id": prereg["dataset_id"],
                    "scope": scope,
                    "mechanism_tags": prereg["proposal"].get("mechanism_tags", []),
                    "field_generation_tags": prereg["proposal"].get("field_generation_tags", []),
                    "variants": prereg["variants"],
                    "failure_signatures": sorted({signature for row in
                        failed_alphas.values() for signature in row["signatures"]}),
                    "diagnosis": diagnosis, "gate": gate,
                    "readback": readback, "preregistration": prereg},
                    batch_dir=batch_dir)
                self._apply_diagnosis_model(diagnosis, model_diagnosis)
            except Exception as exc:
                diagnosis["model_diagnosis"] = {
                    "status": "unavailable", "error_type": type(exc).__name__,
                    "error": str(exc)}
        atomic_write_json(batch_dir / "diagnosis.json", diagnosis)
        attribution = self._decide("attribute_results", {
            "objective": prereg["objective"], "hypothesis": proposal["hypothesis"],
            "dataset_id": prereg["dataset_id"], "scope": scope,
            "mechanism_tags": proposal.get("mechanism_tags", []),
            "field_generation_tags": proposal.get("field_generation_tags", []),
            "failure_criterion": proposal["failure_criterion"],
            "variants": prereg["variants"], "readback": readback, "gate": gate,
            "failure_signatures": {key: row["signatures"]
                                   for key, row in diagnosis["alphas"].items()},
            "diagnosis": diagnosis}, batch_dir=batch_dir)
        if not any(item.get("batch") == str(batch_dir)
                   for item in case.get("batches", [])):
            case.setdefault("batches", []).append({
                "batch": str(batch_dir), "dataset_id": prereg["dataset_id"],
                "hypothesis": prereg["proposal"]["hypothesis"],
                "mechanism_tags": prereg["proposal"].get("mechanism_tags", []),
                "field_generation_tags": prereg["proposal"].get("field_generation_tags", []),
                "expressions": [item["expression"] for item in prereg["variants"]],
                "alpha_ids": alpha_ids,
                "failure_signatures": sorted({signature for row in diagnosis["alphas"].values()
                                               for signature in row["signatures"]}),
                "statuses": {alpha_id: row["status"] for alpha_id, row in gate.items()}})
        atomic_write_json(case_path, case)
        result = {"dataset_id": prereg["dataset_id"], "alpha_ids": alpha_ids,
                  "readback": readback, "attribution": attribution,
                  "gate": gate, "case_path": str(case_path), "scope": scope,
                  "batch_dir": str(batch_dir),
                  "candidate_status": (
                      "CANDIDATE" if any(row["status"] == "CANDIDATE"
                                         for row in gate.values()) else "PENDING_FULL_GATE")}
        context = json.loads((batch_dir / "batch_context.json").read_text(encoding="utf-8"))
        started_at = context.get("started_at")
        try:
            duration_s = max(0, (datetime.now().astimezone() -
                                  datetime.fromisoformat(started_at)).total_seconds())
        except (TypeError, ValueError):
            duration_s = None
        raw_paths = list(batch_dir.glob("probe_*_raw.json")) + [
            batch_dir / "simulation_raw.json"]
        probe_count = len((prereg.get("probe_plan") or {}).get("probes") or [])
        metering = {"started_at": started_at,
                    "finished_at": datetime.now().astimezone().isoformat(),
                    "wall_duration_s": duration_s,
                    "submission_intents": len(list(batch_dir.glob("probe_*_intent.json"))) + 1,
                    "platform_batches": sum(path.exists() for path in raw_paths),
                    "alpha_expressions": len(alpha_ids) + probe_count,
                    "production_expressions": len(alpha_ids),
                    "diagnostic_expressions": probe_count,
                    "retrievals": len(list(batch_dir.glob("*_recovery.json"))) +
                                  int((batch_dir / "recovery.json").exists()),
                    "model_tokens": None}
        atomic_write_json(batch_dir / "metering.json", metering)
        atomic_write_json(batch_dir / "results.json", result)
        atomic_write_json(batch_dir / "simulation_intent.json", {
            "status": "READ_BACK", "prereg_sha256": digest,
            "alpha_ids": alpha_ids, "settings": simulation_settings})
        self._finalize_remedy_outcome(batch_dir, prereg, diagnosis)
        self.campaigns.update(
            case_path.stem, objective=prereg["objective"],
            scope={key: scope[key] for key in ("region", "universe", "delay")},
            current_dataset=prereg["dataset_id"], dataset_state="evaluated",
            current_batch=None, candidate_ledger_ref=str(case_path),
            selection_mode=(prereg.get("dataset_selection") or {}).get("mode"),
            locked=(prereg.get("dataset_selection") or {}).get("mode") == "fixed",
            auth_mode=(self.config.get("auth") or {}).get("mode", "auto"),
            status="batch_complete")
        return result

    def _finalize_remedy_outcome(self, batch_dir, prereg, diagnosis=None):
        """Idempotently record remedy outcomes only after the batch is closed."""
        reference = prereg.get("remedy_reference") or None
        if reference is None:
            return None
        diagnosis_path = Path(batch_dir) / "diagnosis.json"
        if diagnosis is None:
            try:
                diagnosis = json.loads(diagnosis_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ResearchError("closed remedy batch has no readable diagnosis") from exc
        if (diagnosis.get("remedy_outcome") or {}).get("status") == "recorded":
            return diagnosis
        try:
            entry = self.knowledge.record_remedy_outcome(reference, diagnosis, batch_dir)
            batch_ref = str(Path(batch_dir).resolve())
            outcomes = [row for row in entry.get("outcomes", [])
                        if isinstance(row, dict)]
            outcome_index = next((index for index, row in enumerate(outcomes)
                                  if row.get("batch") == batch_ref), None)
            outcome = outcomes[outcome_index] if outcome_index is not None else {}
            uses_after = outcome.get("uses_after")
            if uses_after is None and outcome_index is not None:
                uses_after = outcome_index + 1
            successes_after = outcome.get("successes_after")
            if successes_after is None and outcome_index is not None:
                successes_after = sum(bool(row.get("successful"))
                                       for row in outcomes[:outcome_index + 1])
            status_after = outcome.get("status_after")
            if status_after is None and outcome_index == len(outcomes) - 1:
                status_after = entry.get("status")
            diagnosis["remedy_outcome"] = {
                "status": "recorded", "remedy_id": entry.get("remedy_id"),
                "uses": uses_after, "successes": successes_after,
                "remedy_status": status_after}
        except Exception as exc:
            diagnosis["remedy_outcome"] = {
                "status": "unavailable", "error_type": type(exc).__name__,
                "error": str(exc)}
            atomic_write_json(diagnosis_path, diagnosis)
            raise ResearchError("remedy outcome remains pending; recover this closed batch") from exc
        atomic_write_json(diagnosis_path, diagnosis)
        return diagnosis

    @staticmethod
    def _apply_diagnosis_model(diagnosis, response):
        rows = response.get("alphas") if isinstance(response, dict) else None
        if not isinstance(rows, dict):
            raise ResearchError("diagnose_failures response is missing per-Alpha results")
        expected_alphas = {alpha_id for alpha_id, row in diagnosis["alphas"].items()
                           if row.get("signatures")}
        if set(rows) != expected_alphas:
            raise ResearchError("diagnose_failures Alpha set differs from deterministic failures")
        allowed = {"no_known_remedy", "abandon", "mechanism_review", "use_remedy"}
        for alpha_id, item in diagnosis["alphas"].items():
            if not item.get("signatures"):
                continue
            suggestion = rows.get(alpha_id)
            if not isinstance(suggestion, dict):
                raise ResearchError(f"diagnosis suggestion missing for {alpha_id}")
            recommendation = suggestion.get("recommendation")
            if recommendation not in allowed:
                raise ResearchError(f"invalid diagnosis recommendation for {alpha_id}")
            candidate_ids = {row.get("remedy_id")
                             for row in item.get("remedy_candidates", [])}
            remedy_id = suggestion.get("remedy_id")
            if recommendation == "use_remedy" and remedy_id not in candidate_ids:
                raise ResearchError(f"unverified remedy selected for {alpha_id}")
            if recommendation == "no_known_remedy" and candidate_ids:
                raise ResearchError(f"known remedies exist for {alpha_id}")
            if recommendation != "use_remedy" and remedy_id is not None:
                raise ResearchError(f"unexpected remedy reference for {alpha_id}")
            rationale = suggestion.get("rationale")
            refs = suggestion.get("evidence_refs")
            allowed_refs = {"preregistration", "readback", "gate", "diagnosis_signatures"}
            allowed_refs.update(f"remedy:{key}" for key in candidate_ids)
            if not isinstance(rationale, str) or not rationale.strip():
                raise ResearchError(f"diagnosis rationale missing for {alpha_id}")
            if (not isinstance(refs, list) or not refs or
                    any(ref not in allowed_refs for ref in refs)):
                raise ResearchError(f"diagnosis evidence references invalid for {alpha_id}")
            item["recommendation"] = recommendation
            item["remedy_id"] = remedy_id
            item["model_rationale"] = rationale
            item["evidence_refs"] = refs
        diagnosis["model_diagnosis"] = {"status": "complete", "result": response}

    def recover(self, batch_dir: Path) -> dict:
        self._begin_session()
        self._assert_gate_config()
        self._assert_auth_mode()
        batch_dir = Path(batch_dir).resolve()
        if not batch_dir.is_relative_to(self.runs_root.resolve()):
            raise ResearchError("batch path outside research runs")
        production_intent = batch_dir / "simulation_intent.json"
        if not production_intent.exists():
            return self._recover_probes(batch_dir)
        intent = json.loads(production_intent.read_text(encoding="utf-8"))
        prereg_path = batch_dir / "prereg.json"
        state = json.loads((batch_dir / "state.json").read_text(encoding="utf-8"))
        if not verify_prereg(prereg_path, state):
            raise ResearchError("pre-registration hash mismatch; recovery blocked")
        if intent.get("status") == "READ_BACK":
            result_path = batch_dir / "results.json"
            if not result_path.exists():
                raise ResearchError("READ_BACK intent has no saved results; recovery blocked")
            result = json.loads(result_path.read_text(encoding="utf-8"))
            prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
            simulation_settings = (intent.get("settings") or
                                   prereg.get("simulation_settings") or
                                   self._platform_settings(prereg["scope"]))
            self._record_completed_expressions(
                [item["expression"] for item in prereg["variants"]],
                prereg["dataset_id"], prereg["scope"], simulation_settings)
            case_path = self._case_path(prereg["objective"], prereg["scope"])
            result.setdefault("batch_dir", str(batch_dir))
            self._finalize_remedy_outcome(batch_dir, prereg)
            self.campaigns.update(
                case_path.stem, objective=prereg["objective"],
                scope={key: prereg["scope"][key] for key in ("region", "universe", "delay")},
                current_dataset=prereg["dataset_id"], dataset_state="evaluated",
                current_batch=None, candidate_ledger_ref=str(case_path),
                selection_mode=(prereg.get("dataset_selection") or {}).get("mode"),
                locked=(prereg.get("dataset_selection") or {}).get("mode") == "fixed",
                auth_mode=(self.config.get("auth") or {}).get("mode", "auto"),
                status="batch_complete")
            return result
        expressions = intent["expressions"]
        raw_path = batch_dir / "simulation_raw.json"
        raw = (json.loads(raw_path.read_text(encoding="utf-8"))
               if raw_path.exists() else None)
        if intent.get("status") in {"PREPARED", "STOPPED_BEFORE_SUBMISSION"}:
            prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
            variants = prereg.get("variants")
            runner = intent.get("runner")
            if (intent.get("prereg_sha256") != hashlib.sha256(prereg_path.read_bytes()).hexdigest()
                    or not isinstance(variants, list)
                    or [item.get("expression") for item in variants if isinstance(item, dict)] != expressions
                    or intent.get("settings") != prereg.get("simulation_settings")
                    or not isinstance(runner, dict) or runner.get("batch_id") != batch_dir.name
                    or runner.get("label") != "production"
                    or not all(isinstance(runner.get(key), str) and runner[key]
                               for key in ("case_key", "run_id"))):
                raise ResearchError("unsubmitted simulation evidence changed; recovery blocked")
            if self.stop_flag and self.stop_flag.exists():
                raise ResearchError("stop flag is set; no new simulation posted")
            self.gateway.call("set_case", case_key=runner["case_key"], run_id=runner["run_id"])
            runner_variants = [self._production_runner_variant(
                prereg["proposal"], variant, prereg["dataset_id"], index + 1)
                for index, variant in enumerate(variants)]
            raw = self._simulate(runner_variants, intent["settings"],
                                 prereg["dataset_id"], batch_dir, "production")
            atomic_write_json(raw_path, raw)
        rows = raw.get("alpha_results") if isinstance(raw, dict) else None
        if len(expressions) == 1 and isinstance(raw, dict) and raw.get("alpha_id"):
            rows = [raw]
        if not isinstance(rows, list) or len(rows) != len(expressions):
            raw = self._resume_canonical_batch(intent, batch_dir, raw_path, "production")
            rows = raw.get("alpha_results") if isinstance(raw, dict) else None
        if isinstance(rows, list) and len(rows) == len(expressions):
            alpha_ids = [row.get("alpha_id") for row in rows
                         if isinstance(row, dict) and row.get("alpha_id")]
            if len(alpha_ids) == len(expressions) and len(set(alpha_ids)) == len(alpha_ids):
                prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
                self._record_completed_expressions(
                    expressions, prereg["dataset_id"], prereg["scope"],
                    intent["settings"])
                return self._complete(batch_dir, prereg, alpha_ids,
                                      intent["prereg_sha256"],
                                      intent.get("settings"))
        recent = self._recent_alphas()
        matched, missing, match_notes = match_alphas_detailed(
            expressions, intent["settings"], recent)
        atomic_write_json(batch_dir / "recovery.json", {
            "status": "UNRESOLVED_PROVENANCE",
            "possible_matches": {expr: row.get("id") for expr, row in matched.items()},
            "missing": missing,
            "settings_missing_keys": match_notes})
        raise ResearchError("account matches lack runner provenance; no simulation reposted")

    def _recent_alphas(self):
        recent = []
        for offset in range(0, 20000, 200):
            page = _rows(self.gateway.call("get_user_alphas", stage="IS",
                                           limit=200, offset=offset))
            recent.extend(page)
            if len(page) < 200:
                return recent
        raise ResearchError("account listing incomplete; recovery remains pending")

    def _resume_canonical_batch(self, intent, batch_dir, raw_path, label):
        runner = intent.get("runner") if isinstance(intent, dict) else None
        if not isinstance(runner, dict):
            return None
        try:
            raw = self._normalize_runner_result(
                self.gateway.call("resume_simulation_batch", **runner))
            atomic_write_json(raw_path, raw)
            atomic_write_json(Path(batch_dir) / f"{label}_runner_recovery.json", {
                "status": "RESUMED", "runner": runner,
                "result_rows": len(raw["alpha_results"]),
                "recovered_at": datetime.now().astimezone().isoformat()})
            return raw
        except Exception as exc:
            atomic_write_json(Path(batch_dir) / f"{label}_runner_recovery.json", {
                "status": "UNRESOLVED", "runner": runner,
                "reason": f"{type(exc).__name__}: {str(exc)[:1000]}",
                "recovered_at": datetime.now().astimezone().isoformat()})
            return None

    def _recover_probes(self, batch_dir):
        context_path = batch_dir / "batch_context.json"
        plan_path = batch_dir / "probe_plan.json"
        if not context_path.exists():
            raise ResearchError("probe recovery context is incomplete")
        context = json.loads(context_path.read_text(encoding="utf-8"))
        if (not isinstance(context, dict) or
                any(not isinstance(context.get(key), str) or not context[key].strip()
                    for key in ("platform_case_key", "platform_run_id"))):
            raise ResearchError("probe recovery runner reference is missing; reconcile or void the draft before restarting")
        self.gateway.call("set_case", case_key=context["platform_case_key"],
                          run_id=context["platform_run_id"])
        if not plan_path.exists():
            plan = self._decide("plan_probes", {
                "objective": context["objective"], "scope": context["scope"],
                "dataset_id": context["dataset_id"],
                "fields": _field_summaries(context["fields"])},
                batch_dir=batch_dir)
            atomic_write_json(plan_path, plan)
        else:
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        probes = plan.get("probes")
        if not isinstance(probes, list):
            raise ResearchError("probe plan is unreadable")
        results = []
        recent = None
        for offset in range(0, len(probes), 8):
            group = probes[offset:offset + 8]
            label = f"probe_{offset // 8 + 1}"
            intent_path = batch_dir / f"{label}_intent.json"
            if not intent_path.exists():
                break  # no POST for this group: resume from this point
            intent = json.loads(intent_path.read_text(encoding="utf-8"))
            expressions = [probe["expression"] for probe in group]
            if intent.get("expressions") != expressions:
                raise ResearchError("diagnostic intent differs from saved plan")
            expected_settings = self._platform_settings(context["scope"], probe=True)
            if intent.get("settings") != expected_settings:
                raise ResearchError("diagnostic settings changed; recovery blocked")
            raw_path = batch_dir / f"{label}_raw.json"
            raw = (json.loads(raw_path.read_text(encoding="utf-8"))
                   if raw_path.exists() else None)
            if raw is None and intent.get("status") in {"PREPARED", "STOPPED_BEFORE_SUBMISSION"}:
                runner = intent.get("runner")
                if (not isinstance(runner, dict) or runner.get("case_key") != context["platform_case_key"]
                        or runner.get("run_id") != context["platform_run_id"]
                        or runner.get("batch_id") != batch_dir.name or runner.get("label") != label):
                    raise ResearchError("unsubmitted diagnostic runner evidence changed")
                if self.stop_flag and self.stop_flag.exists():
                    raise ResearchError("stop flag is set; no new simulation posted")
                variants = [{"name": f"probe_{offset + index + 1}",
                             "expression": probe["expression"], "dataset": context["dataset_id"],
                             "research": probe["research"]}
                            for index, probe in enumerate(group)]
                raw = self._simulate(variants, expected_settings, context["dataset_id"], batch_dir, label)
                atomic_write_json(raw_path, raw)
            rows = raw.get("alpha_results") if isinstance(raw, dict) else None
            if len(group) == 1 and isinstance(raw, dict) and raw.get("alpha_id"):
                rows = [raw]
            if not isinstance(rows, list) or len(rows) != len(group):
                raw = self._resume_canonical_batch(intent, batch_dir, raw_path, label)
                rows = raw.get("alpha_results") if isinstance(raw, dict) else None
            if not isinstance(rows, list) or len(rows) != len(group):
                if recent is None:
                    recent = self._recent_alphas()
                matched, missing, match_notes = match_alphas_detailed(
                    expressions, expected_settings, recent)
                atomic_write_json(batch_dir / f"{label}_recovery.json", {
                    "status": "UNRESOLVED_PROVENANCE",
                    "possible_matches": {expr: row.get("id") for expr, row in matched.items()},
                    "missing": missing,
                    "settings_missing_keys": match_notes})
                raise ResearchError("diagnostic account matches lack runner provenance; no simulation reposted")
            alpha_ids = [row.get("alpha_id") for row in rows if isinstance(row, dict)
                         and isinstance(row.get("alpha_id"), str) and row["alpha_id"]]
            if len(alpha_ids) != len(group) or len(set(alpha_ids)) != len(alpha_ids):
                raise ResearchError("diagnostic reconciliation returned invalid Alpha IDs")
            results.extend({"probe": probe, "simulation": row}
                           for probe, row in zip(group, rows))
            self._record_probe_batch(context, expressions, rows,
                                     expected_settings)
            atomic_write_json(intent_path, {
                "status": "READ_BACK", "expressions": expressions,
                "settings": expected_settings, "runner": intent.get("runner")})
        return self.run_once(context["objective"], resume_batch_dir=batch_dir,
                             recovered_probe_results=results, cached_context=context)

    def run_once(self, objective: str, dataset_id: str = None, *,
                 resume_batch_dir: Path = None, recovered_probe_results=None,
                 cached_context=None, fixed_scope=None, diagnosis_id=None,
                 remedy_id=None, allow_remedy_round_overrun=False) -> dict:
        self._begin_session()
        if not objective or not objective.strip():
            raise ResearchError("research objective is required")
        research_type = "PPA" if ppa_requested(objective) else "REGULAR"
        remedy_reference = self._resolve_remedy_reference(
            objective, diagnosis_id, remedy_id, allow_remedy_round_overrun)
        if remedy_reference is not None:
            if dataset_id is not None and dataset_id != remedy_reference["dataset_id"]:
                raise ResearchError("remedy must keep the diagnosis Dataset locked")
            dataset_id = remedy_reference["dataset_id"]
        self._assert_gate_config()
        self._assert_auth_mode()
        if resume_batch_dir is None:
            supplied_dataset = (self.config.get("scope") or {}).get("dataset_id")
            if supplied_dataset and dataset_id and dataset_id != supplied_dataset:
                raise ResearchError("requested Dataset conflicts with configured scope")
            requested_dataset = dataset_id
            dataset_id = supplied_dataset or dataset_id
            unfinished = self._unfinished_batch()
            if unfinished:
                raise ResearchError(f"unfinished batch requires recovery: {unfinished}")
            scope = self._scope(objective, fixed_scope)
            if remedy_reference is not None and any(
                    scope.get(key) != value for key, value in
                    remedy_reference["scope"].items() if value is not None):
                raise ResearchError("remedy must keep the diagnosis Region/Delay/Universe locked")
            prior_case = self._read_case(objective, scope)
            case_path = self._case_path(objective, scope)
            campaign_id = case_path.stem
            self.gateway.call("validate_simulation_settings",
                              settings=self._platform_settings(scope))
            self.gateway.call("validate_simulation_settings",
                              settings=self._platform_settings(scope, probe=True))
            platform_case_key = campaign_id
            platform_run_id = self.recorder.session_id
            self.gateway.call("set_case", case_key=platform_case_key,
                              run_id=platform_run_id)
            self.campaigns.update(
                campaign_id, objective=objective,
                scope={key: scope[key] for key in ("region", "universe", "delay")},
                current_dataset=dataset_id, dataset_state="selecting",
                selection_mode="fixed" if dataset_id else "autonomous",
                locked=bool(dataset_id), current_batch=None,
                candidate_ledger_ref=str(case_path),
                auth_mode=(self.config.get("auth") or {}).get("mode", "auto"),
                status="active")
            context = {k: scope[k] for k in ("region", "universe", "delay")}
            datasets = _rows(self.gateway.call("get_datasets", instrument_type="EQUITY", **context))
            available = {row.get("id") for row in datasets if isinstance(row, dict)}
            if dataset_id is None:
                selection = self._decide("select_dataset", {
                    "objective": objective, "scope": scope, "datasets": datasets,
                    "prior_batches": prior_case.get("batches", [])})
                dataset_id = selection.get("dataset_id")
                if not isinstance(selection.get("reason"), str) or not selection["reason"].strip():
                    raise ResearchError("autonomous Dataset choice needs an economic reason")
                dataset_selection = {"mode": "autonomous", "reason": selection["reason"]}
            else:
                dataset_selection = {"mode": "fixed", "reason": (
                    f"remedy reference {diagnosis_id}/{remedy_id}" if remedy_reference else
                    "configured Dataset" if supplied_dataset else
                    "Dataset specified for this run" if requested_dataset else
                    "fixed Dataset")}
            if dataset_id not in available:
                raise ResearchError(f"Dataset not available in exact scope: {dataset_id}")
            detail = self.gateway.call("get_dataset_details", dataset_id=dataset_id)
            exact_data = [row for row in detail.get("data", [])
                          if isinstance(row, dict)
                          and row.get("region") == scope["region"]
                          and row.get("delay") == scope["delay"]
                          and row.get("universe") == scope["universe"]]
            if detail.get("id") != dataset_id or not exact_data:
                raise ResearchError(f"Dataset detail does not confirm exact scope: {dataset_id}")
            dataset_selection["detail_scope"] = exact_data[0]
            self.campaigns.update(
                campaign_id, current_dataset=dataset_id, dataset_state="selected",
                selection_mode=dataset_selection["mode"],
                locked=dataset_selection["mode"] == "fixed",
                candidate_ledger_ref=str(case_path), status="active")
            fields = _rows(self.gateway.call("get_datafields", instrument_type="EQUITY",
                                             dataset_id=dataset_id, **context))
            operator_rows = _rows(self.gateway.call("get_operators", **context))
            operators = regular_operator_names(operator_rows)
            operator_catalog = [row for row in operator_rows if isinstance(row, dict)
                                and row.get("name") in operators
                                and isinstance(row.get("scope"), list)
                                and "REGULAR" in row["scope"]]
            if not operators:
                raise ResearchError("operator catalog unavailable")
            for value in (scope["region"], scope["universe"], dataset_id):
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
                    raise ResearchError("unsafe scope or Dataset path component")
            batch_dir = (self.runs_root / scope["region"] / f"D{scope['delay']}"
                         / scope["universe"] / dataset_id
                         / (datetime.now().strftime("%Y%m%d_%H%M%S")
                            + "_" + uuid4().hex[:8]))
            batch_dir.mkdir(parents=True)
            atomic_write_json(batch_dir / "batch_context.json", {
                "objective": objective, "research_type": research_type,
                "scope": scope, "dataset_id": dataset_id,
                "platform_case_key": platform_case_key,
                "platform_run_id": platform_run_id,
                "dataset_selection": dataset_selection,
                "fields": fields, "operators": sorted(operators),
                "operator_catalog": operator_rows,
                "started_at": datetime.now().astimezone().isoformat()})
            self.recorder.bind_batch(batch_dir)
            self._write_batch_knowledge_packs(batch_dir)
            self.campaigns.update(campaign_id, current_batch=str(batch_dir),
                                  dataset_state="active", status="active")
            probe_plan = self._decide("plan_probes", {
                "objective": objective, "scope": scope, "dataset_id": dataset_id,
                "fields": _field_summaries(fields),
                "prior_batches": prior_case.get("batches", [])},
                batch_dir=batch_dir)
        else:
            batch_dir = Path(resume_batch_dir).resolve()
            if not batch_dir.is_relative_to(self.runs_root.resolve()):
                raise ResearchError("resume batch path outside research runs")
            if not isinstance(cached_context, dict) or recovered_probe_results is None:
                raise ResearchError("recovery context is incomplete")
            scope = cached_context["scope"]
            dataset_id = cached_context["dataset_id"]
            dataset_selection = cached_context["dataset_selection"]
            fields = cached_context["fields"]
            operators = set(cached_context["operators"])
            operator_catalog = cached_context.get("operator_catalog", [])
            probe_plan = json.loads((batch_dir / "probe_plan.json").read_text(
                encoding="utf-8"))
            prior_case = self._read_case(objective, scope)
            self.recorder.bind_batch(batch_dir)
            self._write_batch_knowledge_packs(batch_dir)
            campaign_id = self._case_path(objective, scope).stem
            platform_case_key = cached_context.get("platform_case_key")
            platform_run_id = cached_context.get("platform_run_id")
            if (not isinstance(platform_case_key, str) or not platform_case_key
                    or not isinstance(platform_run_id, str) or not platform_run_id):
                raise ResearchError("saved batch lacks its local platform case reference")
            self.gateway.call("set_case", case_key=platform_case_key,
                              run_id=platform_run_id)
            self.campaigns.update(
                campaign_id, objective=objective,
                scope={key: scope[key] for key in ("region", "universe", "delay")},
                current_dataset=dataset_id, dataset_state="active",
                selection_mode=dataset_selection.get("mode"),
                locked=dataset_selection.get("mode") == "fixed",
                current_batch=str(batch_dir),
                candidate_ledger_ref=str(self._case_path(objective, scope)),
                auth_mode=(self.config.get("auth") or {}).get("mode", "auto"),
                status="active")
        catalog = _catalog(fields, dataset_id)
        probes = probe_plan.get("probes")
        if not isinstance(probes, list):
            raise ResearchError("model probe plan must contain a list")
        if probes and "NONE" not in scope.get("_supported_neutralizations", []):
            raise ResearchError("diagnostic neutralization NONE is not live supported")
        core_fields = probe_plan.get("core_fields")
        if (not isinstance(core_fields, list) or not core_fields
                or len(set(core_fields)) != len(core_fields)
                or any(field_id not in catalog for field_id in core_fields)):
            raise ResearchError("probe plan must choose live core fields")
        required_dimensions = probe_plan.get("required_dimensions")
        if required_dimensions is None:
            required_dimensions = {field_id: list(FIELD_EVIDENCE_KEYS)
                                   for field_id in core_fields}
        elif (not isinstance(required_dimensions, dict)
              or set(required_dimensions) != set(core_fields)
              or not isinstance(probe_plan.get("requirement_reason"), str)
              or not probe_plan["requirement_reason"].strip()
              or any(not isinstance(items, list) or not items
                     or len(items) != len(set(items))
                     or any(item not in FIELD_EVIDENCE_KEYS for item in items)
                     or "nominal_coverage" not in items
                     or "usable_observations" not in items
                     for items in required_dimensions.values())):
            raise ResearchError("required field diagnostics need a justified dimension list")
        field_rows = {row["id"]: row for row in fields
                      if isinstance(row, dict) and row.get("id") in core_fields}
        atomic_write_json(batch_dir / "probe_plan.json", probe_plan)
        probe_results = [] if recovered_probe_results is None else recovered_probe_results
        if probes and len(probe_results) < len(probes):
            for probe in probes:
                if not isinstance(probe, dict) or not all(probe.get(k) for k in
                    ("field_id", "expression", "question", "measures", "economic_relevance",
                     "expected", "failure_criterion")):
                    raise ResearchError("each diagnostic probe needs a question and ex-ante criterion")
                if not self._has_complete_runner_research(probe.get("research")):
                    raise ResearchError("each diagnostic probe needs complete canonical runner research evidence")
                if (probe["field_id"] not in core_fields or not isinstance(probe["measures"], list)
                        or any(item not in FIELD_EVIDENCE_KEYS for item in probe["measures"])):
                    raise ResearchError("probe field or measured property is outside the admitted pool")
                verdict = validate(probe["expression"], dataset_id, catalog, operators,
                                   operator_definitions=operator_catalog)
                if verdict["verdict"] != "PASS":
                    raise ResearchError(f"probe expression rejected: {verdict['errors']}")
            probe_settings = self._platform_settings(scope, probe=True)
            if len(probe_results) % 8:
                raise ResearchError("recovered diagnostic prefix is incomplete")
            self._ensure_expressions_uncached(
                [probe["expression"] for probe in probes[len(probe_results):]],
                dataset_id, scope,
                probe_settings, batch_dir, label="diagnostic probes")
            for offset in range(len(probe_results), len(probes), 8):
                group = probes[offset:offset + 8]
                expressions = [probe["expression"] for probe in group]
                label = f"probe_{offset // 8 + 1}"
                runner_ref = {"case_key": platform_case_key,
                              "run_id": platform_run_id,
                              "batch_id": batch_dir.name, "label": label}
                if not runner_ref["case_key"] or not runner_ref["run_id"]:
                    raise ResearchError("diagnostic batch lacks its local platform case reference")
                simulation_variants = [
                    {"name": f"probe_{offset + index + 1}",
                     "expression": probe["expression"], "dataset": dataset_id,
                     "research": probe["research"]}
                    for index, probe in enumerate(group)]
                atomic_write_json(batch_dir / f"{label}_intent.json", {
                    "status": "PREPARED", "expressions": expressions,
                    "settings": self._platform_settings(scope, probe=True),
                    "runner": runner_ref})
                raw_probe = self._simulate(
                    simulation_variants, self._platform_settings(scope, probe=True),
                    dataset_id, batch_dir, label)
                atomic_write_json(batch_dir / f"{label}_raw.json", raw_probe)
                if not isinstance(raw_probe, dict) or raw_probe.get("error"):
                    raise ResearchError("diagnostic simulation unresolved; do not resubmit")
                rows = raw_probe.get("alpha_results") or []
                if len(group) == 1 and raw_probe.get("alpha_id"):
                    rows = [raw_probe]
                if len(rows) != len(group):
                    raise ResearchError("diagnostic children unresolved; do not resubmit")
                alpha_ids = [row.get("alpha_id") for row in rows if isinstance(row, dict)
                             and isinstance(row.get("alpha_id"), str)
                             and row["alpha_id"]]
                if len(alpha_ids) != len(group) or len(set(alpha_ids)) != len(alpha_ids):
                    raise ResearchError("diagnostic children unresolved; do not mark READ_BACK")
                probe_results.extend({"probe": probe, "simulation": row}
                                     for probe, row in zip(group, rows))
                self._record_probe_batch({
                    "dataset_id": dataset_id, "scope": scope}, expressions, rows,
                    probe_settings)
                atomic_write_json(batch_dir / f"{label}_intent.json", {
                    "status": "READ_BACK", "expressions": expressions,
                    "settings": probe_settings, "runner": runner_ref})
        sources = {field_id: {dimension: set() for dimension in FIELD_EVIDENCE_KEYS}
                   for field_id in core_fields}
        probe_observations = []
        probe_reports = []
        for field_id in core_fields:
            for dimension, key in FIELD_EVIDENCE_KEYS.items():
                if field_rows[field_id].get(key) is not None:
                    sources[field_id][dimension].add("catalog")
        for index, item in enumerate(probe_results):
            simulation = item["simulation"]
            if not isinstance(simulation, dict) or not simulation.get("alpha_id"):
                continue
            probe = item["probe"]
            probe_source = (batch_dir / f"probe_{index // 8 + 1}_raw.json").resolve()
            report = {
                "probe_id": f"probe:{index}",
                "field_id": probe.get("field_id"),
                "field_type": catalog.get(probe.get("field_id"), {}).get("type"),
                "measures": probe.get("measures", []),
                "expression": probe.get("expression"),
                "question": probe.get("question"),
                "economic_relevance": probe.get("economic_relevance"),
                "expected": probe.get("expected"),
                "failure_criterion": probe.get("failure_criterion"),
                "alpha_id": simulation.get("alpha_id"),
                "source": f"{probe_source}#alpha_id={simulation.get('alpha_id')}",
                "simulation": {"alpha_id": simulation.get("alpha_id"),
                               "details": simulation.get("details")},
            }
            probe_reports.append(report)
            details = simulation.get("details")
            is_data = details.get("is") if isinstance(details, dict) else None
            counts = None
            if isinstance(is_data, dict):
                long_count, short_count = (is_data.get("longCount"),
                                           is_data.get("shortCount"))
                if (all(isinstance(value, (int, float)) and not isinstance(value, bool)
                        and value >= 0 for value in (long_count, short_count))):
                    counts = {"long_count": long_count, "short_count": short_count}
            for dimension in probe["measures"]:
                if counts is None:
                    continue
                if dimension == "nominal_coverage" or (
                        dimension == "usable_observations" and
                        re.search(r"(?:!=|==)\s*0\b", probe["expression"])):
                    sources[probe["field_id"]][dimension].add(f"probe:{index}")
                    observation = {"source": report["source"],
                                   "probe_id": report["probe_id"],
                                   "field_id": probe["field_id"],
                                   "dimension": dimension,
                                   "observation": counts,
                                   "limit": "stock counts are approximate and depend on handling"}
                    probe_observations.append(observation)
                    report.setdefault("observations", []).append(observation)
        atomic_write_json(batch_dir / "probe_evidence.json", {
            "schema_version": 1, "dataset_id": dataset_id,
            "scope": {key: scope.get(key) for key in ("region", "delay", "universe")},
            "observations": probe_reports})
        missing = [(field_id, dimension) for field_id in core_fields
                   for dimension in required_dimensions[field_id]
                   if not sources[field_id][dimension]]
        if missing:
            raise ResearchError(f"field diagnostics unresolved; batch construction blocked: {missing}")

        construction_context = {
            "objective": objective, "research_type": research_type,
            "ppa_rules": ({"max_operators": PPA_MAX_OPERATORS,
                           "max_data_fields": PPA_MAX_FIELDS, "min_sharpe": PPA_MIN_SHARPE}
                          if research_type == "PPA" else None),
            "scope": scope, "dataset_id": dataset_id,
            "fields": _field_summaries([field_rows[field_id] for field_id in core_fields
                                         if field_id in field_rows]),
            "probe_plan": probe_plan,
            "core_fields": core_fields,
            "operator_catalog": operator_catalog,
            "remedy_reference": remedy_reference,
            "prior_batches": prior_case.get("batches", []),
            "probe_results": probe_results, "probe_observations": probe_observations,
            "available_evidence": {
                field_id: {dimension: sorted(values) for dimension, values in dimensions.items()}
                for field_id, dimensions in sources.items()}}
        proposal = self._decide("construct_batch", construction_context,
                                batch_dir=batch_dir)
        proposed_expressions = [row.get("expression") for row in proposal.get("variants", [])
                                if isinstance(row, dict) and isinstance(row.get("expression"), str)]
        used_operators = sorted({name for expression in proposed_expressions
                                 for name in re.findall(
                                     r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", expression)})
        draft_pack = self.knowledge.build_pack("construct_batch", {
            **construction_context, "proposal": proposal,
            "hypothesis": proposal.get("hypothesis", ""),
            "operator_names": used_operators})
        exact_operator_notes = draft_pack["l2_operator_notes"]
        related_evidence = draft_pack["l3_evidence"]
        if exact_operator_notes or related_evidence:
            proposal = self._decide("construct_batch", {
                **construction_context, "draft_proposal": proposal,
                "proposal": proposal, "operator_names": used_operators,
                "exact_operator_notes": exact_operator_notes,
                "related_evidence": related_evidence},
                batch_dir=batch_dir)
        if remedy_reference is not None:
            proposed_tags = proposal.get("mechanism_tags")
            if isinstance(proposed_tags, str):
                proposed_tags = [proposed_tags]
            if (not isinstance(proposed_tags, list) or
                    any(not isinstance(item, str) for item in proposed_tags) or
                    sorted(proposed_tags) != sorted(remedy_reference["mechanism_tags"])):
                raise ResearchError("remedy batch must preserve the diagnosed mechanism tags")
            for variant in proposal.get("variants") or []:
                reason = variant.get("reason", "") if isinstance(variant, dict) else ""
                if (not isinstance(reason, str) or remedy_id not in reason or
                        diagnosis_id not in reason):
                    raise ResearchError("each remedy variant must cite diagnosis_id and remedy_id")
        variants = proposal.get("variants")
        if not isinstance(variants, list) or not 1 <= len(variants) <= 8:
            raise ResearchError("batch must contain one to eight variants")
        if not all(isinstance(proposal.get(key), str) and proposal[key].strip()
                   for key in ("hypothesis", "expected_sign",
                               "structure_window_rationale", "data_rationale",
                               "operator_rationale", "field_relationship",
                               "settings_rationale", "failure_criterion")):
            raise ResearchError("canonical runner research rationales and failure criterion are required")
        reports = proposal.get("field_report")
        if not isinstance(reports, list):
            raise ResearchError("structured field report is required")
        report_by_field = {row.get("field_id"): row for row in reports
                           if isinstance(row, dict) and row.get("field_id")}
        expressions = []
        runner_variants = []
        operator_counts = {}
        validation_results = {}
        for variant in variants:
            expr = variant.get("expression") if isinstance(variant, dict) else None
            if (not isinstance(expr, str) or not isinstance(variant.get("reason"), str)
                    or not variant["reason"].strip()):
                raise ResearchError("each variant needs an expression and ex-ante reason")
            verdict = validate(expr, dataset_id, catalog, operators,
                               operator_definitions=operator_catalog,
                               ppa=research_type == "PPA")
            if verdict["verdict"] != "PASS":
                raise ResearchError(f"expression rejected: {verdict['errors']}")
            validation_results[expr] = verdict
            for field_id in verdict["fields"]:
                row = report_by_field.get(field_id)
                if field_id not in core_fields or not row or row.get("verdict") != "ADMIT":
                    raise ResearchError(f"field was not admitted by diagnostic report: {field_id}")
                if not isinstance(row.get("constraints"), list) or not row["constraints"]:
                    raise ResearchError(f"field constraints missing: {field_id}")
                evidence = row.get("evidence") or {}
                unresolved = row.get("unresolved") or {}
                for dimension, available in sources[field_id].items():
                    item = evidence.get(dimension)
                    if item is not None:
                        if (not isinstance(item, dict) or item.get("source") not in available
                                or not item.get("observation")):
                            raise ResearchError(f"unsupported field observation: {field_id}/{dimension}")
                    elif dimension in required_dimensions[field_id]:
                        raise ResearchError(f"required field observation missing: {field_id}/{dimension}")
                    else:
                        gap = unresolved.get(dimension) if isinstance(unresolved, dict) else None
                        if (not isinstance(gap, dict) or not gap.get("reason")
                                or not gap.get("why_not_material")):
                            raise ResearchError(f"unresolved field gap not explained: {field_id}/{dimension}")
            expressions.append(expr)
            runner_variants.append(self._production_runner_variant(
                proposal, variant, dataset_id, len(runner_variants) + 1))
            operator_counts[expr] = verdict["operator_count"]
        if len(set(expressions)) != len(expressions):
            reason = "duplicate expressions in batch"
            self.void_batch(batch_dir, reason)
            raise ResearchError(reason)
        prior_expressions = {expression for item in prior_case.get("batches", [])
                             if item.get("dataset_id") == dataset_id
                             for expression in item.get("expressions", [])}
        if prior_expressions.intersection(expressions):
            reason = "expression already tested in this case and Dataset"
            self.void_batch(batch_dir, reason)
            raise ResearchError(reason)
        simulation_settings = self._platform_settings(scope)
        self._ensure_expressions_uncached(
            expressions, dataset_id, scope, simulation_settings,
            batch_dir, label="production simulation")

        mechanism_tags = proposal.get("mechanism_tags")
        if isinstance(mechanism_tags, str):
            mechanism_tags = [mechanism_tags]
        if not isinstance(mechanism_tags, list) or not any(
                isinstance(item, str) and item.strip() for item in mechanism_tags):
            mechanism_tags = [re.sub(r"\s+", " ", proposal["hypothesis"]).strip()[:100]]
        generation_tags = proposal.get("field_generation_tags")
        if isinstance(generation_tags, str):
            generation_tags = [generation_tags]
        if not isinstance(generation_tags, list) or not any(
                isinstance(item, str) and item.strip() for item in generation_tags):
            generation_tags = sorted({f"{field_id}:{catalog[field_id]['type']}"
                                      for expr in expressions
                                      for field_id in validation_results[expr]["fields"]})
        proposal["mechanism_tags"] = [item.strip() for item in mechanism_tags
                                      if isinstance(item, str) and item.strip()]
        proposal["field_generation_tags"] = [item.strip() for item in generation_tags
                                             if isinstance(item, str) and item.strip()]

        prereg = {"objective": objective, "research_type": research_type,
                  "dataset_id": dataset_id,
                  "dataset_selection": dataset_selection,
                  "scope": scope, "fields": fields, "probe_plan": probe_plan,
                  "proposal": proposal, "variants": variants,
                  "simulation_settings": simulation_settings,
                  "operator_counts": operator_counts,
                  "validation": validation_results,
                  "remedy_reference": ({key: value for key, value in
                                        remedy_reference.items() if key != "remedy"} | {
                                            "remedy": remedy_reference["remedy"]}
                                       if remedy_reference else None)}
        prereg_path = batch_dir / "prereg.json"
        atomic_write_json(prereg_path, prereg)
        state = {}
        digest = lock_prereg(prereg_path, state)
        atomic_write_json(batch_dir / "state.json", state)
        if not verify_prereg(prereg_path, state):
            raise ResearchError("pre-registration changed before simulation")
        atomic_write_json(batch_dir / "simulation_intent.json", {
            "status": "PREPARED", "prereg_sha256": digest,
            "expressions": expressions, "settings": simulation_settings,
            "runner": {"case_key": platform_case_key,
                       "run_id": platform_run_id,
                       "batch_id": batch_dir.name, "label": "production"}})

        raw = self._simulate(runner_variants, simulation_settings,
                             dataset_id, batch_dir, "production")
        atomic_write_json(batch_dir / "simulation_raw.json", raw)
        if not isinstance(raw, dict) or raw.get("error"):
            raise ResearchError("simulation unresolved; inspect intent and reconcile before retrying")
        alpha_rows = raw.get("alpha_results") or []
        if len(expressions) == 1 and raw.get("alpha_id"):
            alpha_rows = [raw]
        alpha_ids = [row.get("alpha_id") for row in alpha_rows if isinstance(row, dict)
                     and row.get("alpha_id")]
        if len(alpha_ids) != len(expressions) or len(set(alpha_ids)) != len(alpha_ids):
            raise ResearchError("simulation children unresolved; do not resubmit this batch")
        self._record_completed_expressions(expressions, dataset_id, scope,
                                           simulation_settings)
        return self._complete(batch_dir, prereg, alpha_ids, digest,
                              simulation_settings)

    def run_case(self, objective: str, max_batches: int, dataset_id: str = None, *,
                 diagnosis_id=None, remedy_id=None,
                 allow_remedy_round_overrun=False) -> dict:
        """Run a bounded number of serial research batches in one case."""
        if not isinstance(max_batches, int) or isinstance(max_batches, bool) or max_batches < 1:
            raise ResearchError("max_batches must be a positive integer")
        if (diagnosis_id is not None or remedy_id is not None) and max_batches != 1:
            raise ResearchError("a remedy reference admits exactly one new batch per command")
        new_alpha_ids = []
        ppa_eligibility = {}
        candidate_datasets = []
        case_path = None
        fixed_scope = None
        for number in range(max_batches):
            result = self.run_once(objective, dataset_id=dataset_id,
                                   fixed_scope=fixed_scope,
                                   diagnosis_id=diagnosis_id, remedy_id=remedy_id,
                                   allow_remedy_round_overrun=allow_remedy_round_overrun)
            diagnosis_id = remedy_id = None
            allow_remedy_round_overrun = False
            if fixed_scope is None:
                fixed_scope = {key: result["scope"][key] for key in
                               ("region", "universe", "delay", "decay",
                                "neutralization", "truncation", "pasteurization",
                                "unit_handling", "nan_handling", "test_period",
                                "max_trade")}
            new_alpha_ids.extend(result["alpha_ids"])
            if ppa_eligibility:
                reaudits_path = Path(result["batch_dir"]) / "retained_candidate_audits.json"
                try:
                    reaudits = json.loads(reaudits_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    reaudits = {}
                if not isinstance(reaudits, dict):
                    reaudits = {}
                for alpha_id in ppa_eligibility:
                    audit = reaudits.get(alpha_id)
                    latest = audit.get("ppa_eligibility") if isinstance(audit, dict) else None
                    ppa_eligibility[alpha_id] = (latest if isinstance(latest, dict) else {
                        "status": "PENDING", "operator_count": None, "field_count": None,
                        "sharpe": None, "failures": [],
                        "pending": ["latest_ppa_eligibility_unavailable"]})
            ppa_eligibility.update({alpha_id: verdict["ppa_eligibility"]
                                    for alpha_id, verdict in result["gate"].items()
                                    if "ppa_eligibility" in verdict})
            case_path = Path(result["case_path"])
            case = json.loads(case_path.read_text(encoding="utf-8"))
            dataset_by_batch = {item["batch"]: item["dataset_id"]
                                for item in case.get("batches", [])}
            candidate_datasets = sorted({dataset_by_batch.get(item.get("batch"))
                                         for item in case.get("contenders", {}).values()
                                         if item.get("status") == "CANDIDATE"
                                         and dataset_by_batch.get(item.get("batch"))})
            if len(candidate_datasets) >= 3:
                status = "TARGET_REACHED"
                break
            if number + 1 == max_batches:
                status = "BATCH_LIMIT_REACHED"
                break
            decision = self._decide("decide_next", {
                "objective": objective, "dataset_id": result["dataset_id"],
                "scope": result["scope"], "last_result": {
                    "dataset_id": result["dataset_id"],
                    "gate": result["gate"],
                    "attribution": result["attribution"]},
                "prior_batches": case.get("batches", []),
                "candidate_datasets": candidate_datasets},
                batch_dir=result.get("batch_dir"))
            if (decision.get("action") not in {"continue", "pause"}
                    or not isinstance(decision.get("reason"), str)
                    or not decision["reason"].strip()):
                raise ResearchError("next research decision is incomplete")
            atomic_write_json(case_path.parent / f"{case_path.stem}_decision.json",
                              {"after_batch": case["batches"][-1]["batch"],
                               **decision})
            if decision["action"] == "pause":
                status = "MODEL_PAUSED"
                break
        if case_path is not None:
            self.campaigns.update(
                case_path.stem, status=status.lower(), current_batch=None,
                candidate_ledger_ref=str(case_path), dataset_state=status.lower())
        return {"status": status, "batches_completed": number + 1,
                "new_alpha_ids": new_alpha_ids,
                "candidate_datasets": candidate_datasets,
                "case_path": str(case_path),
                **({"ppa_eligibility": ppa_eligibility} if ppa_eligibility else {})}
