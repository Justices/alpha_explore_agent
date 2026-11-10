"""Independent BRAIN API session, catalogs, simulation and recovery.

All simulation state is owned by this project. A POST is never retried after
an uncertain response; recovery only polls the saved platform location.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urljoin, urlsplit

import requests

from alpha_research.qualification.correlation import pairwise_pnl_correlation, parse_pnl_recordset
from alpha_research.qualification.gates import (local_self_correlation_state, parse_correlations,
                         ra_correlation_check_state, terminal_correlation_check)
from alpha_research.progress import write_progress
from alpha_research.state.paths import CATALOG_SNAPSHOT_ROOT, default_state_root, expand_path
from alpha_research.simulation.matching import SETTING_KEYS
from alpha_research.simulation.cache import BacktestCache, BacktestCacheError
from alpha_research.simulation.platform_cache import cache_record, history_payload
from alpha_research.simulation.tags import diagnostic_properties, research_tags
from alpha_research.state.store import atomic_write_json, file_sha256, read_json
from alpha_research.state.records import ResearchRecordStore
from alpha_research.state.operation_lock import SubmissionGate
from alpha_research.state.writer_lock import SingleWriterLock
from alpha_research.expressions.validator import regular_operator_names, validate


class PlatformError(RuntimeError):
    pass


def _component(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise PlatformError(f"invalid {label}")
    return value


class WorldQuantClient:
    """One in-memory session using injected credentials or the selected file."""
    base_url = "https://api.worldquantbrain.com"

    def __init__(self, session=None, *, credentials=None):
        self._credentials = None
        self._account = None
        if credentials is not None:
            if (not isinstance(credentials, dict)
                    or not all(isinstance(credentials.get(key), str) and credentials[key].strip()
                               for key in ("username", "password"))):
                raise PlatformError("BRAIN credentials input is invalid")
            self._credentials = credentials["username"], credentials["password"]
        self.session = session or requests.Session()
        self.authenticated = False

    def _url(self, path):
        url = urljoin(self.base_url + "/", path)
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname != "api.worldquantbrain.com"
                or parts.port not in (None, 443) or parts.username or parts.password):
            raise PlatformError("platform URL has an unexpected origin")
        return url

    def _file_credentials(self):
        source = os.environ.get("BRAIN_CREDENTIALS_FILE")
        if not source:
            raise PlatformError("BRAIN_CREDENTIALS_FILE is not configured")
        try:
            data = json.loads(expand_path(source).read_text(encoding="utf-8-sig"))
            nested = data.get("credentials") or {}
            if not isinstance(nested, dict):
                raise ValueError("invalid credentials object")
            email = nested.get("email") or data.get("email")
            password = nested.get("password") or data.get("password")
            if not isinstance(email, str) or not email or not isinstance(password, str) or not password:
                raise ValueError("incomplete credentials")
        except (OSError, ValueError, AttributeError, TypeError) as exc:
            raise PlatformError("BRAIN credentials file is unavailable or invalid") from exc
        return email, password

    def _auth(self):
        auth = self._credentials or self._file_credentials()
        if self._account is not None and self._account != auth[0]:
            raise PlatformError("BRAIN account changed during the platform session")
        self._account = auth[0]
        return auth

    def _login(self):
        auth = self._auth()
        try:
            response = self.session.post(self.base_url + "/authentication",
                                         auth=auth, timeout=40,
                                         allow_redirects=False)
        except requests.RequestException as exc:
            raise PlatformError(f"BRAIN authentication failed: {type(exc).__name__}") from exc
        if response.status_code not in (200, 201):
            raise PlatformError(f"BRAIN authentication failed: HTTP {response.status_code}")
        self.authenticated = True

    @property
    def catalog_account(self):
        return self._account if self._account is not None else self._auth()[0]

    def request(self, method, path, *, params=None, json=None):
        if not self.authenticated:
            self._login()
        url = self._url(path)
        try:
            response = self.session.request(method, url, params=params, json=json, timeout=40,
                                            allow_redirects=False)
            if response.status_code == 401 and method in {"GET", "OPTIONS"}:
                self.authenticated = False
                self._login()
                response = self.session.request(method, url, params=params, json=json, timeout=40,
                                                allow_redirects=False)
        except requests.RequestException as exc:
            raise PlatformError(f"platform request failed: {type(exc).__name__}") from exc
        return response

    def close(self):
        self.session.close()


class PlatformRuntime:
    """The agent's own platform adapter; it does not load other repositories."""
    BATCH_LIMIT = 8
    CATALOG_MAX_AGE = timedelta(days=7)
    ALPHA_HISTORY_CAP = 1000

    def __init__(self, state_root=None, *, client=None, poll_timeout=1200):
        self.state_root = Path(state_root or os.environ.get("NASTU_QUANT_STATE_DIR")
                               or default_state_root() / "state").resolve()
        self.root = self.state_root / "platform"
        self.client = client or WorldQuantClient()
        self.poll_timeout = poll_timeout
        self.case_key = self.run_id = self.case_root = self.run_root = None
        self._local_self_results = {}
        self._recent_alpha_details = {}
        self._prefilter_batch = None
        self._prefilter_cache = {}
        self._snapshot_root = CATALOG_SNAPSHOT_ROOT
        self._account_namespaces = {}

    def close(self):
        self.client.close()

    def set_case(self, case_key, run_id=None):
        case_key = _component(case_key, "case key")
        run_id = _component(run_id, "run ID")
        if (case_key, run_id) != (self.case_key, self.run_id):
            self._local_self_results = {}
            self._recent_alpha_details = {}
            self._prefilter_batch = None
            self._prefilter_cache = {}
        self.case_key, self.run_id = case_key, run_id
        self.case_root = self.root / "cases" / self.case_key
        self.run_root = self.case_root / "sessions" / self.run_id
        self.run_root.mkdir(parents=True, exist_ok=True)
        return {"case_key": self.case_key, "run_id": self.run_id,
                "case_root": str(self.case_root), "run_root": str(self.run_root)}

    def _require_case(self):
        if self.run_root is None:
            raise PlatformError("research case has not been selected")
        return self.run_root

    def _request(self, method, path, **kwargs):
        for attempt in range(3):
            try:
                response = self.client.request(method, path, **kwargs)
            except PlatformError:
                raise
            except Exception as exc:
                raise PlatformError(f"platform request failed: {type(exc).__name__}") from exc
            if not isinstance(getattr(response, "status_code", None), int):
                raise PlatformError("platform returned an unreadable HTTP response")
            if response.status_code == 429 and method in {"GET", "OPTIONS"} and attempt < 2:
                try:
                    delay = max(0.1, min(30.0, float(response.headers.get("Retry-After", 3))))
                except (TypeError, ValueError):
                    delay = 3.0
                time.sleep(delay)
                continue
            if not 200 <= response.status_code < 300:
                raise PlatformError(f"platform request returned HTTP {response.status_code}")
            return response

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except (ValueError, TypeError) as exc:
            raise PlatformError("platform response is not readable JSON") from exc

    def _paginate(self, path, params):
        rows, offset, total, identifiers = [], 0, None, set()
        while offset <= 100000:
            page = self._json(self._request("GET", path, params={**params, "limit": 50, "offset": offset}))
            if not isinstance(page, dict) or not isinstance(page.get("results"), list):
                raise PlatformError("platform catalog page is unreadable")
            count = page.get("count")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise PlatformError("platform catalog count is unavailable")
            if total is None:
                total = count
            if count != total:
                raise PlatformError("platform catalog changed during pagination")
            current = page["results"]
            if not current and offset < total:
                raise PlatformError("platform catalog is incomplete")
            if any(not isinstance(row, dict) or not isinstance(row.get("id"), str)
                   or not row["id"] for row in current):
                raise PlatformError("platform catalog contains invalid records")
            page_ids = {row["id"] for row in current}
            if len(page_ids) != len(current) or page_ids.intersection(identifiers):
                raise PlatformError("platform catalog contains duplicate records")
            identifiers.update(page_ids)
            rows.extend(current)
            offset += len(current)
            if offset == total:
                return rows
            if offset > total:
                raise PlatformError("platform catalog contains too many rows")
        raise PlatformError("platform catalog exceeds pagination limit")

    @staticmethod
    def _scope(params):
        region = _component(params.get("region"), "region")
        universe = _component(params.get("universe"), "universe")
        delay = params.get("delay")
        instrument = params.get("instrument_type", "EQUITY")
        if type(delay) is not int or delay not in (0, 1) or instrument != "EQUITY":
            raise PlatformError("exact EQUITY Region, Universe and Delay are required")
        return {"instrumentType": instrument, "region": region,
                "universe": universe, "delay": delay}

    def _settings_schema(self):
        response = self._json(self._request("OPTIONS", "/simulations"))
        try:
            schema = response["actions"]["POST"]["settings"]["children"]
        except (KeyError, TypeError) as exc:
            raise PlatformError("platform simulation settings schema is unavailable") from exc
        if not isinstance(schema, dict):
            raise PlatformError("platform simulation settings schema is unreadable")
        return schema

    def _setting_options(self):
        schema = self._settings_schema()
        try:
            types = schema["instrumentType"]["choices"]
            regions = schema["region"]["choices"]["instrumentType"]
            delays = schema["delay"]["choices"]["instrumentType"]
            universes = schema["universe"]["choices"]["instrumentType"]
            neutral = schema["neutralization"]["choices"]["instrumentType"]
            rows = []
            for kind in types:
                kind = kind["value"]
                for region in regions[kind]:
                    region = region["value"]
                    for delay in delays[kind]["region"][region]:
                        rows.append({"InstrumentType": kind, "Region": region,
                                     "Delay": delay["value"],
                                     "Universe": [x["value"] for x in universes[kind]["region"][region]],
                                     "Neutralization": [x["value"] for x in neutral[kind]["region"][region]]})
        except (KeyError, TypeError, IndexError) as exc:
            raise PlatformError("platform scope choices are unreadable") from exc
        if not rows:
            raise PlatformError("platform scope choices are empty")
        return {"instrument_options": rows, "total_combinations": len(rows)}

    @staticmethod
    def _catalog_layout(relative):
        if relative.parts[0] == "fields":
            return Path("data_fields", *relative.parts[1:])
        if relative == Path("catalogs", "operators.json"):
            return Path("operator", relative.name)
        return Path("datasets", *relative.parts[1:])

    def _account_namespace(self, key_path=None):
        key_path = key_path or self.root / ".catalog-snapshot-key"
        if key_path not in self._account_namespaces:
            account = getattr(self.client, "catalog_account", None)
            if not isinstance(account, str) or not account:
                return None
            key_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(os.urandom(32))
            key = key_path.read_bytes()
            if len(key) != 32:
                raise PlatformError("catalog snapshot identity key is unreadable")
            self._account_namespaces[key_path] = hmac.new(
                key, account.encode("utf-8"), hashlib.sha256).hexdigest()
        return self._account_namespaces[key_path]

    def _catalog_snapshot_path(self, relative):
        return self._snapshot_root / self._catalog_layout(relative)

    def _platform_backtest_cache(self):
        namespace = self._account_namespace()
        if namespace is None:
            raise PlatformError("BRAIN account identity is required for the backtest cache")
        return BacktestCache(self.root / "backtests" / namespace / "completed.cache")

    def _cached_catalog(self, path, expected, rows_key):
        if path is None:
            return None, None, "缓存不存在"
        try:
            payload = path.read_bytes()
            snapshot = json.loads(payload.decode("utf-8"))
        except FileNotFoundError:
            return None, None, "缓存不存在"
        except (OSError, ValueError):
            return None, None, "缓存无效或无法读取"
        if not isinstance(snapshot, dict):
            return None, None, "缓存内容或范围校验失败"
        metadata, rows = snapshot.get("metadata"), snapshot.get(rows_key)
        account = self._account_namespace(self._snapshot_root / ".catalog-snapshot-key")
        if isinstance(metadata, dict) and (account is None or metadata.get("account_namespace") != account):
            return None, None, "缓存账号校验不通过"
        identity = "name" if rows_key == "operators" else "id"
        if (not isinstance(metadata, dict)
                or any(metadata.get(name) != value for name, value in expected.items())
                or metadata.get("complete") is not True
                or not isinstance(rows, list) or type(metadata.get("count")) is not int
                or metadata["count"] != len(rows)
                or any(not isinstance(row, dict) or not isinstance(row.get(identity), str)
                       or not row[identity].strip() for row in rows)
                or (not rows if rows_key == "operators"
                    else len({row["id"] for row in rows}) != len(rows))):
            return None, None, "缓存内容或范围校验失败"
        try:
            fetched = datetime.fromisoformat(metadata["fetched_at"])
            if fetched.utcoffset() is None:
                return None, None, "快照时间缺少时区"
            age = datetime.now(timezone.utc) - fetched
            if age < timedelta(0):
                return None, None, "快照时间晚于当前时间"
            if age > self.CATALOG_MAX_AGE:
                return None, None, "缓存已过期"
        except (KeyError, ValueError, TypeError):
            return None, None, "快照时间无效"
        return snapshot, hashlib.sha256(payload).hexdigest(), None

    def _catalog(self, command, params):
        root = self._require_case()
        if command == "get_operators":
            label = "算子目录"
            endpoint, rows_key = "/operators", "operators"
            relative = Path("catalogs", "operators.json")
            expected = {"source": self.client.base_url + endpoint}
        else:
            scope = self._scope(params)
            scope_path = Path(scope["region"], str(scope["delay"]), scope["universe"])
            dataset = (_component(params.get("dataset_id"), "Dataset ID")
                       if command == "get_datafields" else None)
            scope_label = f"{scope['region']} / D{scope['delay']} / {scope['universe']}"
            label = f"字段目录 {scope_label} / {dataset}" if dataset else f"数据集目录 {scope_label}"
            endpoint = "/data-fields" if dataset else "/data-sets"
            rows_key = "fields" if dataset else "rows"
            relative = (Path("fields") / scope_path / f"{dataset}.json" if dataset
                        else Path("catalogs") / scope_path / "datasets.json")
            expected = {"region": scope["region"], "delay": scope["delay"],
                        "universe": scope["universe"], "instrument_type": scope["instrumentType"],
                        "source": self.client.base_url + endpoint}
            if dataset:
                expected["dataset"] = dataset
        account = self._account_namespace(self._snapshot_root / ".catalog-snapshot-key")
        if account is None:
            raise PlatformError("BRAIN account identity is required for catalog snapshots")
        cache_path = self._catalog_snapshot_path(relative)
        snapshot, digest, reason = ((None, None, "请求强制刷新") if params.get("refresh") is True
                            else self._cached_catalog(cache_path, expected, rows_key))
        if snapshot is None:
            write_progress(f"{label} · {reason}，正在向 BRAIN 拉取完整目录")
            try:
                if rows_key == "operators":
                    rows = self._operator_rows(self._json(self._request("GET", endpoint)))
                else:
                    query = {**scope, "dataset.id": dataset} if dataset else scope
                    rows = self._paginate(endpoint, query)
            except PlatformError:
                write_progress(f"{label} · BRAIN 拉取失败")
                raise
            metadata = {**expected, "account_namespace": account,
                        "complete": True, "count": len(rows),
                        "fetched_at": datetime.now(timezone.utc).isoformat()}
            snapshot = {"metadata": metadata, rows_key: rows}
            if rows_key == "rows":
                snapshot["scope"] = scope
            try:
                atomic_write_json(cache_path, snapshot)
            except OSError:
                write_progress(f"{label} · 快照写入失败 · {cache_path}")
                raise
            digest = file_sha256(cache_path)
            event = "快照已写入"
        else:
            event = "命中缓存"
        write_progress(f"{label} · {event} · {snapshot['metadata']['count']} 条 · "
                       f"拉取时间 {snapshot['metadata']['fetched_at']} · {cache_path}")
        atomic_write_json(root / relative.with_suffix(".ref.json"), {
            "snapshot_ref": str(cache_path), "sha256": digest,
            "fetched_at": snapshot["metadata"]["fetched_at"]})
        return snapshot[rows_key]

    @staticmethod
    def _operator_rows(payload):
        rows = payload
        if isinstance(payload, dict):
            rows = (None if payload.get("error") else next(
                (payload[key] for key in ("operators", "results", "data", "items")
                 if isinstance(payload.get(key), list)), None))
        if (not isinstance(rows, list) or not rows
                or any(not isinstance(row, dict) or not isinstance(row.get("name"), str)
                       or not row["name"].strip() for row in rows)):
            raise PlatformError("platform operator catalog is unavailable")
        return [{**row, "scope": [row["scope"]]} if isinstance(row.get("scope"), str)
                else row for row in rows]

    @staticmethod
    def validate_simulation_settings(settings):
        required = ("instrument_type", "region", "universe", "delay", "decay", "neutralization",
                    "truncation", "pasteurization", "unit_handling", "nan_handling",
                    "language", "visualization", "test_period", "max_trade")
        if not isinstance(settings, dict) or any(settings.get(key) is None for key in required):
            raise PlatformError("complete effective simulation settings are required")
        if settings["instrument_type"] != "EQUITY" or settings["language"] != "FASTEXPR":
            raise PlatformError("only explicitly validated EQUITY FASTEXPR settings are supported")
        if set(settings) - set(required):
            raise PlatformError("unsupported simulation setting cannot be preserved")
        _component(settings["region"], "region")
        _component(settings["universe"], "universe")
        for name in ("neutralization", "pasteurization", "unit_handling", "nan_handling", "max_trade"):
            _component(settings[name], name)
        if type(settings["delay"]) is not int or settings["delay"] not in (0, 1):
            raise PlatformError("invalid simulation delay")
        if type(settings["decay"]) is not int or settings["decay"] < 0:
            raise PlatformError("invalid simulation decay")
        truncation = settings["truncation"]
        try:
            valid_truncation = (isinstance(truncation, (int, float)) and not isinstance(truncation, bool)
                                and math.isfinite(truncation) and 0 <= truncation <= 1)
        except OverflowError:
            valid_truncation = False
        if not valid_truncation:
            raise PlatformError("invalid simulation truncation")
        if type(settings["visualization"]) is not bool:
            raise PlatformError("invalid simulation visualization setting")
        if not isinstance(settings["test_period"], str) or not settings["test_period"].strip():
            raise PlatformError("invalid simulation test period")
        return {"compatible": True, "entry_point": "local_platform_runtime"}

    @staticmethod
    def _platform_settings(settings):
        return {SETTING_KEYS.get(key, key): value for key, value in settings.items()}

    def _batch_paths(self, batch_id, label, case_key=None, run_id=None):
        if case_key is not None or run_id is not None:
            self.set_case(case_key or self.case_key, run_id or self.run_id)
        root = self._require_case()
        if not isinstance(batch_id, str) or not re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{8}", batch_id):
            raise PlatformError("invalid batch ID")
        if not isinstance(label, str) or not re.fullmatch(r"(?:production|super|probe_[1-9][0-9]*)", label):
            raise PlatformError("invalid batch label")
        folder = root / "batches"
        stem = f"{batch_id}_{label}"
        output = folder / f"{stem}.json"
        return {"request": folder / f"{stem}.request.json", "output": output,
                "receipt": output.with_suffix(".admission.json"),
                "progress": folder / f"{stem}.progress.json",
                "post_started": folder / f"{stem}.post_started.json"}

    @staticmethod
    def _load(path):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PlatformError(f"local evidence is unavailable: {path.name}") from exc

    def _admission_catalog(self, relative, expected, rows_key, label):
        root = self._require_case()
        ref_path = root / relative.with_suffix(".ref.json")
        reference = self._load(ref_path)
        path = self._catalog_snapshot_path(relative)
        snapshot, digest, _ = self._cached_catalog(path, expected, rows_key)
        if (not isinstance(reference, dict) or snapshot is None
                or reference.get("snapshot_ref") != str(path)
                or reference.get("sha256") != digest):
            raise PlatformError(f"{label} snapshot changed or expired after research admission")
        return snapshot

    def _admission(self, request):
        settings = request["settings"]
        relative = (Path("fields") / settings["region"] / str(settings["delay"])
                    / settings["universe"] / f"{request['dataset_id']}.json")
        expected = {"region": settings["region"], "delay": settings["delay"],
                    "universe": settings["universe"], "instrument_type": settings["instrument_type"],
                    "dataset": request["dataset_id"], "complete": True}
        snapshot = self._admission_catalog(
            relative, {**expected, "source": self.client.base_url + "/data-fields"},
            "fields", "field")
        metadata, fields = snapshot.get("metadata"), snapshot.get("fields")
        if (not isinstance(metadata, dict) or any(metadata.get(k) != v for k, v in expected.items())
                or not isinstance(fields, list) or metadata.get("count") != len(fields)):
            raise PlatformError("exact-scope field snapshot is unavailable")
        operators = self._admission_catalog(
            Path("catalogs", "operators.json"), {"source": self.client.base_url + "/operators"},
            "operators", "operator").get("operators")
        if not isinstance(operators, list) or not operators:
            raise PlatformError("operator snapshot is unavailable")
        names = regular_operator_names(operators)
        if not names:
            raise PlatformError("REGULAR operator scope evidence is unavailable")
        catalog = {}
        by_id = {}
        for row in fields:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise PlatformError("field snapshot is malformed")
            owner = row.get("dataset")
            owner = owner.get("id") if isinstance(owner, dict) else owner
            if owner == request["dataset_id"] and row.get("type") in {"MATRIX", "VECTOR"} and row.get("description"):
                catalog[row["id"]] = {"dataset": owner, "type": row["type"]}
                by_id[row["id"]] = row
        admissions = []
        for variant in request["variants"]:
            if not isinstance(variant, dict) or variant.get("dataset", request["dataset_id"]) != request["dataset_id"]:
                raise PlatformError("simulation batch mixes Datasets")
            verdict = validate(variant.get("expression"), request["dataset_id"], catalog, names,
                               operator_definitions=operators)
            if verdict.get("verdict") != "PASS" or not verdict.get("fields"):
                raise PlatformError("expression failed local field or operator validation")
            fields_used = [by_id[field] for field in verdict["fields"]]
            if any(not isinstance(row.get("coverage"), (int, float)) or isinstance(row.get("coverage"), bool)
                   or not math.isfinite(row["coverage"]) or not 0 <= row["coverage"] <= 1 for row in fields_used):
                raise PlatformError("field coverage evidence is unavailable")
            research = variant.get("research")
            required = ("idea", "data_rationale", "operator_rationale", "field_relationship",
                        "settings_rationale", "expected_behavior", "failure_criterion")
            if not isinstance(research, dict) or any(not isinstance(research.get(k), str) or not research[k].strip() for k in required):
                raise PlatformError("pre-backtest research evidence is incomplete")
            admissions.append({"passed": True, "dataset": request["dataset_id"],
                "scope": {"region": settings["region"], "delay": settings["delay"],
                          "universe": settings["universe"], "instrument_type": settings["instrument_type"]},
                "fields": fields_used, "failures": [], "validation": verdict})
        return admissions

    def _metadata(self, paths):
        progress = self._load(paths["progress"])
        return {"case_key": self.case_key, "run_id": self.run_id,
                "field_cache_root": str(self._snapshot_root / "data_fields"),
                "output": str(paths["output"]), "progress": str(paths["progress"]),
                "resume_url": progress.get("resume_url"), "status": progress.get("status")}

    def run_simulation_batch(self, params):
        paths = self._batch_paths(params.get("batch_id"), params.get("label"))
        request = {"case_key": self.case_key, "run_id": self.run_id,
                   "batch_id": params.get("batch_id"), "label": params.get("label"),
                   "dataset_id": _component(params.get("dataset_id"), "Dataset ID"),
                   "settings": params.get("settings"), "variants": params.get("variants")}
        if params.get("origin_alpha_id") is not None:
            request["origin_alpha_id"] = _component(params["origin_alpha_id"], "origin Alpha ID")
        self.validate_simulation_settings(request["settings"])
        variants = request["variants"]
        if not isinstance(variants, list) or not 1 <= len(variants) <= self.BATCH_LIMIT:
            raise PlatformError("simulation batch requires one to eight variants")
        if (any(not isinstance(row, dict) or not isinstance(row.get("name"), str)
                or not row["name"].strip() or not isinstance(row.get("expression"), str)
                or not row["expression"].strip() for row in variants)
                or len({row["name"].strip() for row in variants}) != len(variants)
                or len({row["expression"].strip() for row in variants}) != len(variants)):
            raise PlatformError("simulation variants must have unique names and expressions")
        if paths["progress"].exists():
            if (paths["output"].exists() and self._load(paths["request"]) == request
                    and self._load(paths["progress"]).get("status") == "COMPLETE"):
                return self.resume_simulation_batch({"case_key": self.case_key,
                    "run_id": self.run_id, "batch_id": request["batch_id"],
                    "label": request["label"]})
            raise PlatformError("prior simulation may have been submitted; recover without reposting")
        admissions = self._admission(request)
        settings = self._platform_settings(request["settings"])
        receipt = {"dataset": request["dataset_id"], "settings": settings,
                   "variants": variants, "field_admissions": admissions,
                   "created_at": datetime.now(timezone.utc).isoformat()}
        try:
            with SubmissionGate(self.state_root):
                if paths["progress"].exists() or paths["post_started"].exists():
                    raise PlatformError("prior simulation may have been submitted; recover without reposting")
                atomic_write_json(paths["request"], request)
                atomic_write_json(paths["receipt"], receipt)
                progress = {"status": "PREPARED", "resume_url": None,
                            "request_sha256": hashlib.sha256(paths["request"].read_bytes()).hexdigest(),
                            "receipt_sha256": hashlib.sha256(paths["receipt"].read_bytes()).hexdigest()}
                self._post_prepared(request, paths, progress)
            return self._finish(request, paths, progress)
        except Exception as exc:
            if isinstance(exc, PlatformError):
                raise
            raise PlatformError(f"simulation is unresolved: {type(exc).__name__}") from exc

    def run_super_simulation(self, params):
        """Run one BRAIN-native Super simulation with the same recovery ledger."""
        paths = self._batch_paths(params.get("batch_id"), "super")
        settings = params.get("settings")
        self.validate_super_settings(settings)
        selection, combo = params.get("selection"), params.get("combo")
        if any(not isinstance(code, str) or not code.strip()
               for code in (selection, combo)):
            raise PlatformError("Super selection and combo expressions are required")
        research = params.get("research")
        if (not isinstance(research, dict) or
                any(not isinstance(research.get(key), str) or
                    not research[key].strip() for key in
                    ("hypothesis", "rationale", "failure_criterion"))):
            raise PlatformError("pre-backtest Super research evidence is incomplete")
        request = {"case_key": self.case_key, "run_id": self.run_id,
                   "batch_id": params["batch_id"], "label": "super",
                   "type": "SUPER", "settings": settings,
                   "variants": [{"name": params.get("name") or "super_candidate",
                                 "selection": selection, "combo": combo}],
                   "research": research}
        if paths["progress"].exists():
            if (paths["output"].exists() and self._load(paths["request"]) == request
                    and self._load(paths["progress"]).get("status") == "COMPLETE"):
                return self.resume_simulation_batch({
                    "case_key": self.case_key, "run_id": self.run_id,
                    "batch_id": request["batch_id"], "label": "super"})
            raise PlatformError("prior Super simulation may have been submitted; recover")
        receipt = {"type": "SUPER", "settings": self._platform_settings(settings),
                   "selection": selection, "combo": combo, "research": research,
                   "created_at": datetime.now(timezone.utc).isoformat()}
        try:
            with SubmissionGate(self.state_root):
                if paths["progress"].exists() or paths["post_started"].exists():
                    raise PlatformError("prior Super simulation may have been submitted; recover")
                atomic_write_json(paths["request"], request)
                atomic_write_json(paths["receipt"], receipt)
                progress = {"status": "PREPARED", "resume_url": None,
                            "request_sha256": hashlib.sha256(
                                paths["request"].read_bytes()).hexdigest(),
                            "receipt_sha256": hashlib.sha256(
                                paths["receipt"].read_bytes()).hexdigest()}
                self._post_prepared(request, paths, progress)
            return self._finish(request, paths, progress)
        except Exception as exc:
            if isinstance(exc, PlatformError):
                raise
            raise PlatformError(f"Super simulation is unresolved: {type(exc).__name__}") from exc

    def validate_super_settings(self, settings):
        if not isinstance(settings, dict):
            raise PlatformError("Super settings must be an object")
        regular = {key: settings.get(key) for key in
                   ("instrument_type", "region", "universe", "delay", "decay",
                    "neutralization", "truncation", "pasteurization",
                    "unit_handling", "nan_handling", "language",
                    "visualization", "test_period", "max_trade")}
        self.validate_simulation_settings(regular)
        if set(settings) - set(regular) - {
                "selectionHandling", "selectionLimit", "componentActivation"}:
            raise PlatformError("unsupported Super setting cannot be preserved")
        schema = self._settings_schema()
        for key in ("selectionHandling", "componentActivation"):
            rule = schema.get(key)
            choices = rule.get("choices") if isinstance(rule, dict) else None
            allowed = {row.get("value") for row in choices if isinstance(row, dict)} if isinstance(choices, list) else set()
            if settings.get(key) not in allowed:
                raise PlatformError(f"{key} is not a live supported Super setting")
        rule = schema.get("selectionLimit")
        limit = settings.get("selectionLimit")
        if (not isinstance(rule, dict) or type(limit) is not int
                or not isinstance(rule.get("minValue"), (int, float))
                or limit < rule["minValue"]
                or isinstance(rule.get("maxValue"), (int, float))
                and limit > rule["maxValue"]):
            raise PlatformError("selectionLimit is not a live supported Super setting")
        combos = self._setting_options()["instrument_options"]
        if not any(row.get("InstrumentType") == regular["instrument_type"]
                   and row.get("Region") == regular["region"]
                   and row.get("Delay") == regular["delay"]
                   and regular["universe"] in row.get("Universe", [])
                   and regular["neutralization"] in row.get("Neutralization", [])
                   for row in combos):
            raise PlatformError("Super scope is not a live supported combination")
        return {"compatible": True, "entry_point": "native_super_runtime"}

    def _post_prepared(self, request, paths, progress):
        """Start a proven-unsubmitted request while holding the submission gate."""
        if paths["post_started"].exists():
            raise PlatformError("simulation may have been submitted; do not repost")
        if (self.state_root / "stop.flag").exists():
            progress["status"] = "STOPPED_BEFORE_SUBMISSION"
            progress["unsubmitted_proof"] = "submission_gate_v1"
            atomic_write_json(paths["progress"], progress)
            raise PlatformError("stop flag is set; no new simulation posted")
        payloads = self._simulation_payloads(request)
        if request.get("type") != "SUPER":
            self._assert_platform_uncached(payloads)
        atomic_write_json(paths["post_started"], {"request_sha256": progress["request_sha256"],
                                                  "receipt_sha256": progress["receipt_sha256"]})
        progress.pop("unsubmitted_proof", None)
        progress["status"] = "SUBMITTING"
        atomic_write_json(paths["progress"], progress)
        response = self._request("POST", "/simulations", json=payloads[0] if len(payloads) == 1 else payloads)
        location = response.headers.get("Location") or response.headers.get("location")
        if not isinstance(location, str) or not location:
            raise PlatformError("simulation response has no recovery location")
        self._simulation_path(location)
        progress.update({"status": "SUBMITTED", "resume_url": location})
        atomic_write_json(paths["progress"], progress)

    def _simulation_payloads(self, request):
        settings = self._platform_settings(request["settings"])
        if request.get("type") == "SUPER":
            return [{"type": "SUPER", "settings": settings,
                     "selection": row["selection"], "combo": row["combo"]}
                    for row in request["variants"]]
        return [{"type": "REGULAR", "settings": settings, "regular": row["expression"]}
                for row in request["variants"]]

    def _assert_platform_uncached(self, payloads):
        try:
            cache = self._platform_backtest_cache()
            records = [cache_record(payload) for payload in payloads]
            fingerprints = {cache.fingerprint(**record) for record in records}
            if len(fingerprints) != len(records):
                raise PlatformError("simulation request contains duplicate cache keys")
            if any(cache.find(**record) for record in records):
                raise PlatformError("platform backtest cache possible hit; no simulation posted")
        except BacktestCacheError as exc:
            raise PlatformError("platform backtest cache is unavailable") from exc

    def _record_platform_backtest(self, request):
        try:
            with SubmissionGate(self.state_root):
                cache = self._platform_backtest_cache()
                cache.record_many([cache_record(payload)
                                   for payload in self._simulation_payloads(request)])
        except BacktestCacheError as exc:
            raise PlatformError("completed platform backtest cache could not be updated") from exc

    def check_simulation_cache(self, params):
        settings = params.get("settings")
        if not isinstance(settings, dict):
            raise PlatformError("simulation cache check requires fixed settings")
        expressions = params.get("expressions")
        if (not isinstance(expressions, list) or not expressions
                or any(not isinstance(expr, str) or not expr.strip() for expr in expressions)):
            raise PlatformError("Regular cache check requires expressions")
        request = {"settings": settings,
                   "variants": [{"expression": expr} for expr in expressions]}
        self._assert_platform_uncached(self._simulation_payloads(request))
        return {"status": "clear"}

    def _history_stage(self, stage, cutoff):
        endpoint = "/users/self/alphas"

        def query(upper, lower=None):
            params = {"stage": stage, "order": "dateCreated",
                      "dateCreated<": upper.isoformat().replace("+00:00", "Z")}
            if lower is not None:
                after = lower - timedelta(microseconds=1)
                params["dateCreated>"] = after.isoformat().replace("+00:00", "Z")
            return params

        def head(params):
            page = self._json(self._request("GET", endpoint, params={
                **params, "limit": 1, "offset": 0}))
            count = page.get("count") if isinstance(page, dict) else None
            rows = page.get("results") if isinstance(page, dict) else None
            if type(count) is not int or count < 0 or not isinstance(rows, list) or len(rows) != min(1, count):
                raise PlatformError("platform Alpha history count is unreadable")
            return count, rows

        params = query(cutoff)
        count, first = head(params)
        if count < self.ALPHA_HISTORY_CAP:
            rows = self._paginate(endpoint, params) if count else []
            if len(rows) != count:
                raise PlatformError("platform Alpha history count changed")
            yield from rows
            return
        try:
            oldest = datetime.fromisoformat(first[0]["dateCreated"].replace("Z", "+00:00"))
            if oldest.utcoffset() is None or oldest >= cutoff:
                raise ValueError("invalid history window")
            oldest = oldest.astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise PlatformError("oldest Alpha timestamp is unavailable") from exc
        windows = [(oldest, cutoff)]
        while windows:
            lower, upper = windows.pop()
            params = query(upper, lower)
            count, _ = head(params)
            if count >= self.ALPHA_HISTORY_CAP:
                middle = lower + (upper - lower) / 2
                if not lower < middle < upper:
                    raise PlatformError("platform Alpha history exceeds the cap at one timestamp")
                windows.extend(((middle, upper), (lower, middle)))
                continue
            rows = self._paginate(endpoint, params) if count else []
            if len(rows) != count:
                raise PlatformError("platform Alpha history count changed")
            yield from rows

    def sync_backtest_cache(self):
        cutoff = datetime.now(timezone.utc)
        summary = {"cutoff": cutoff.isoformat(), "scanned": {}, "indexed": 0,
                   "skipped": 0, "duplicate_across_stages": 0}
        records, seen = [], set()
        for stage in ("IS", "OS"):
            stage_ids = set()
            for alpha in self._history_stage(stage, cutoff):
                alpha_id = _component(alpha.get("id"), "Alpha ID")
                if alpha_id in stage_ids:
                    raise PlatformError(f"duplicate Alpha in {stage} history")
                stage_ids.add(alpha_id)
                if alpha_id in seen:
                    summary["duplicate_across_stages"] += 1
                    continue
                seen.add(alpha_id)
                payload = history_payload(alpha)
                if payload is None and alpha.get("type") in {None, "REGULAR"}:
                    detail = self._json(self._request("GET", f"/alphas/{alpha_id}"))
                    if not isinstance(detail, dict) or detail.get("id") != alpha_id:
                        raise PlatformError("platform Alpha history readback mismatch")
                    payload = history_payload(detail)
                if payload is None:
                    summary["skipped"] += 1
                else:
                    records.append(cache_record(payload))
            summary["scanned"][stage] = len(stage_ids)
        with SubmissionGate(self.state_root):
            cache = self._platform_backtest_cache()
            summary["indexed"] = sum(cache.record_many(records))
            summary["eligible"] = len(records)
            summary["status"] = "complete"
            atomic_write_json(cache.path.with_name("last_sync.json"), summary)
        return summary

    @staticmethod
    def _simulation_path(value):
        if not isinstance(value, str):
            raise PlatformError("invalid simulation recovery location")
        parsed = urlsplit(value)
        if parsed.scheme or parsed.netloc:
            if (parsed.scheme != "https" or parsed.hostname != "api.worldquantbrain.com"
                    or parsed.port not in (None, 443) or parsed.username or parsed.password
                    or parsed.query or parsed.fragment):
                raise PlatformError("invalid simulation recovery origin")
            value = parsed.path
        if not re.fullmatch(r"/simulations/[A-Za-z0-9_-]+", value):
            raise PlatformError("invalid simulation recovery location")
        return value

    def _poll(self, location):
        path = self._simulation_path(location)
        deadline = time.monotonic() + self.poll_timeout
        while time.monotonic() < deadline:
            response = self._request("GET", path)
            try:
                data = self._json(response)
            except PlatformError:
                if response.status_code not in (202, 204) and not response.headers.get("Retry-After"):
                    raise
                data = {}
            if data is None and (response.status_code in (202, 204) or response.headers.get("Retry-After")):
                data = {}
            if not isinstance(data, dict):
                raise PlatformError("simulation status is unreadable")
            status = str(data.get("status") or "").upper()
            if status in {"ERROR", "FAIL", "FAILED", "CANCELLED", "CANCELED", "REJECTED", "KILLED"}:
                raise PlatformError(f"simulation terminated with status {status}")
            if data.get("alpha") or data.get("children"):
                return data
            try:
                wait = max(0.1, min(30.0, float(response.headers.get("Retry-After", 2))))
            except (TypeError, ValueError):
                wait = 2.0
            time.sleep(wait)
        raise PlatformError("simulation polling timed out; recover the saved location")

    def _finish(self, request, paths, progress):
        data = self._poll(progress["resume_url"])
        variants = request["variants"]
        if len(variants) == 1:
            ids = [data.get("alpha")]
        else:
            children = data.get("children")
            if not isinstance(children, list) or len(children) != len(variants):
                raise PlatformError("simulation children are incomplete; recovery required")
            ids = []
            for child in children:
                location = child.get("location") or child.get("id") if isinstance(child, dict) else child
                if isinstance(location, str) and not location.startswith(("/", "https://")):
                    location = "/simulations/" + location
                ids.append(self._poll(location).get("alpha"))
        if any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in ids) or len(set(ids)) != len(ids):
            raise PlatformError("simulation Alpha IDs are incomplete; recovery required")
        rows = []
        for variant, alpha_id in zip(variants, ids):
            details = self._json(self._request("GET", "/alphas/" + alpha_id))
            if not isinstance(details, dict) or details.get("id") != alpha_id:
                raise PlatformError("simulation Alpha readback is unavailable")
            if request.get("type") == "SUPER":
                selected = details.get("selection")
                combined = details.get("combo")
                actual_settings = details.get("settings")
                expected_settings = self._platform_settings(request["settings"])
                if (details.get("type") != "SUPER"
                        or not isinstance(selected, dict)
                        or selected.get("code") != variant["selection"]
                        or not isinstance(combined, dict)
                        or combined.get("code") != variant["combo"]
                        or not isinstance(actual_settings, dict)
                        or any(actual_settings.get(key) != value
                               for key, value in expected_settings.items())):
                    raise PlatformError("Super Alpha readback does not match intent")
                rows.append({"name": variant.get("name"),
                             "selection": variant["selection"], "combo": variant["combo"],
                             "alpha_id": alpha_id, "success": True,
                             "is": details.get("is"), "details": details})
            else:
                rows.append({"name": variant.get("name"), "expression": variant["expression"],
                             "alpha_id": alpha_id, "success": True, "is": details.get("is"),
                             "details": details})
        regular = request.get("type") != "SUPER"
        if regular and not request["label"].startswith("probe_"):
            tags = research_tags(request)
            for row in rows:
                row["research_tags"] = {"status": "planned", "tags": tags}
                atomic_write_json(self._tag_plan_path(row["alpha_id"]), {
                    "alpha_id": row["alpha_id"], "tags": tags,
                    "family": request.get("type", "REGULAR"),
                    "request_ref": str(paths["request"]),
                    "request_sha256": progress["request_sha256"],
                })
        atomic_write_json(paths["output"], rows)
        if regular:
            self._record_platform_backtest(request)
        progress["output_sha256"] = hashlib.sha256(paths["output"].read_bytes()).hexdigest()
        progress["status"] = "COMPLETE"
        atomic_write_json(paths["progress"], progress)
        return {"results": rows, "runner": self._metadata(paths)}

    def _tag_plan_path(self, alpha_id):
        namespace = self._account_namespace()
        if namespace is None:
            raise PlatformError("BRAIN account identity is required for tag plans")
        return self.root / "tag_plans" / namespace / f"{alpha_id}.json"

    def tag_alpha(self, params):
        alpha_id = _component(params.get("alpha_id"), "Alpha ID")
        plan_path = self._tag_plan_path(alpha_id)
        plan = self._load(plan_path)
        if not isinstance(plan, dict) or plan.get("alpha_id") != alpha_id:
            raise PlatformError("Alpha tag plan does not match the requested Alpha")
        request_path = Path(plan.get("request_ref", "")).resolve()
        if (not request_path.is_relative_to(self.root / "cases")
                or not request_path.is_file() or file_sha256(request_path) != plan.get("request_sha256")):
            raise PlatformError("Alpha tag plan research evidence is unavailable")
        output = request_path.with_suffix("").with_suffix(".json")
        progress = self._load(output.with_suffix(".progress.json"))
        if (not isinstance(progress, dict) or progress.get("status") != "COMPLETE"
                or progress.get("request_sha256") != plan["request_sha256"]
                or not output.is_file() or progress.get("output_sha256") != file_sha256(output)):
            raise PlatformError("Alpha tag plan has no completed simulation evidence")
        rows = self._load(output)
        if (not isinstance(rows, list)
                or sum(isinstance(row, dict) and row.get("alpha_id") == alpha_id
                       and row.get("success") is True for row in rows) != 1):
            raise PlatformError("Alpha tag plan does not match the completed simulation")
        endpoint = f"/alphas/{alpha_id}"
        details = self._json(self._request("GET", endpoint))
        if (not isinstance(details, dict) or details.get("id") != alpha_id
                or details.get("type") != plan.get("family")):
            raise PlatformError("Alpha tag plan readback does not match research evidence")
        existing = details.get("tags", [])
        if not isinstance(existing, list) or any(not isinstance(tag, str) for tag in existing):
            raise PlatformError("existing Alpha tags are unreadable")
        desired = research_tags(self._load(request_path))
        if any(isinstance(row, dict) and row.get("name") == "Power Pool Alpha"
               for row in (details.get("classifications") or [])):
            desired.append("PowerPoolSelected")
        changes = {}
        merged = existing + [tag for tag in desired if tag not in existing]
        if merged != existing:
            changes["tags"] = merged
        if params.get("diagnostic") is True:
            if ra_correlation_check_state(details) != "pass":
                raise PlatformError("diagnostic tag requires current terminal RA evidence")
            records = [row for row in ResearchRecordStore(self.state_root.parent).list("candidate")
                       if row.get("alpha_id") == alpha_id]
            diagnostic = diagnostic_properties(max(records, key=lambda row: row["updated_at"])
                                               if records else None)
            if not diagnostic:
                raise PlatformError("diagnostic tag requires settled local or production correlation evidence")
            current_name = details.get("name") or ""
            if current_name and not current_name.startswith(("pc_", "sc_")):
                diagnostic.pop("name", None)
            changes.update({key: value for key, value in diagnostic.items()
                            if details.get(key) != value})
        if params.get("apply") is not True:
            return {"alpha_id": alpha_id, "status": "preview", "changes": changes}
        if changes:
            self._request("PATCH", endpoint, json=changes)
            latest = self._json(self._request("GET", endpoint))
            verified_tags = latest.get("tags") if isinstance(latest, dict) else None
            if (not isinstance(latest, dict) or latest.get("id") != alpha_id
                    or any(latest.get(key) != value for key, value in changes.items()
                           if key != "tags")
                    or not isinstance(verified_tags, list)
                    or any(not isinstance(tag, str) for tag in verified_tags)
                    or not set(merged).issubset(verified_tags)):
                raise PlatformError("Alpha tag write could not be verified by readback")
        result = {"alpha_id": alpha_id, "status": "applied" if changes else "already_applied",
                  "changes": changes}
        atomic_write_json(plan_path.with_suffix(".receipt.json"), result)
        return result

    def resume_simulation_batch(self, params):
        paths = self._batch_paths(params.get("batch_id"), params.get("label"),
                                  params.get("case_key"), params.get("run_id"))
        request, progress = self._load(paths["request"]), self._load(paths["progress"])
        if (request.get("case_key") != self.case_key or request.get("run_id") != self.run_id
                or progress.get("request_sha256") != hashlib.sha256(paths["request"].read_bytes()).hexdigest()
                or progress.get("receipt_sha256") != hashlib.sha256(paths["receipt"].read_bytes()).hexdigest()):
            raise PlatformError("simulation recovery evidence changed")
        if progress.get("status") == "STOPPED_BEFORE_SUBMISSION":
            try:
                with SubmissionGate(self.state_root):
                    latest = self._load(paths["progress"])
                    if (latest.get("request_sha256") != hashlib.sha256(paths["request"].read_bytes()).hexdigest()
                            or latest.get("receipt_sha256") != hashlib.sha256(paths["receipt"].read_bytes()).hexdigest()):
                        raise PlatformError("simulation recovery evidence changed")
                    request = self._load(paths["request"])
                    if latest.get("status") == "STOPPED_BEFORE_SUBMISSION":
                        if latest.get("unsubmitted_proof") != "submission_gate_v1":
                            raise PlatformError("simulation may have been submitted; do not repost")
                        self._post_prepared(request, paths, latest)
                    progress = latest
            except PlatformError:
                raise
            except Exception as exc:
                raise PlatformError(f"simulation recovery is unresolved: {type(exc).__name__}") from exc
        if paths["output"].exists() and progress.get("status") == "COMPLETE":
            if progress.get("output_sha256") != hashlib.sha256(paths["output"].read_bytes()).hexdigest():
                raise PlatformError("completed simulation result changed; recovery blocked")
            return {"results": self._load(paths["output"]), "runner": self._metadata(paths)}
        if not progress.get("resume_url"):
            raise PlatformError("simulation may have been submitted but has no recovery location; do not repost")
        return self._finish(request, paths, progress)

    def _save_pnl_evidence(self, alpha_id, payload):
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        path = self._require_case() / "correlations" / f"{alpha_id}_{digest}.json"
        if path.exists():
            saved = json.dumps(self._load(path), ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
            if hashlib.sha256(saved).hexdigest() != digest:
                raise PlatformError("saved PnL evidence changed")
        else:
            atomic_write_json(path, payload)
        return {"path": str(path), "sha256": digest}

    def _get_pnl(self, alpha_id, cache=None):
        pnls = cache.setdefault("pnls", {}) if cache is not None else {}
        if alpha_id not in pnls:
            payload = self._json(self._request("GET", f"/alphas/{alpha_id}/recordsets/pnl"))
            if cache is None or not parse_pnl_recordset(payload):
                return payload
            pnls[alpha_id] = payload
        return pnls[alpha_id]

    def _local_self_correlation(self, alpha_id, limit, *, cache=None):
        """Compare the Alpha against the complete same-region non-PPAC OS pool."""
        alpha_id = _component(alpha_id, "Alpha ID")
        try:
            valid_limit = (not isinstance(limit, bool) and isinstance(limit, (int, float))
                           and math.isfinite(limit) and 0 <= limit <= 1)
        except OverflowError:
            valid_limit = False
        if not valid_limit:
            raise PlatformError("local self-correlation limit is unavailable")
        self._require_case()
        result = {"status": "pending", "value": None, "complete": False,
                  "comparison_count": 0, "window_years": 4, "limit": limit,
                  "missing": [], "comparisons": []}
        try:
            details = self._json(self._request("GET", f"/alphas/{alpha_id}"))
            settings = details.get("settings") if isinstance(details, dict) else None
            region = settings.get("region") if isinstance(settings, dict) else None
            if not isinstance(details, dict) or details.get("id") != alpha_id or not isinstance(region, str) or not region:
                raise PlatformError("Alpha region is unavailable")
            ra_state = ra_correlation_check_state(details)
            result["ra_state"] = ra_state
            if ra_state != "pass":
                result["ra_details"] = details
            if ra_state != "pass":
                result["reason"] = "ra_checks_" + ra_state
                return result
            self._recent_alpha_details[alpha_id] = details
            pools = cache.setdefault("pools", {}) if cache is not None else {}
            region_key = region.upper()
            if region_key not in pools:
                pool = self._paginate("/users/self/alphas", {"stage": "OS", "order": "-dateSubmitted"})
                ids = []
                for item in pool:
                    item_settings = item.get("settings")
                    other_region = item_settings.get("region") if isinstance(item_settings, dict) else None
                    if not isinstance(other_region, str) or not other_region:
                        raise PlatformError("OS pool region is unavailable")
                    if other_region.upper() != region_key:
                        continue
                    classifications = item.get("classifications")
                    if (not isinstance(classifications, list) or any(
                            not isinstance(row, dict) or not isinstance(row.get("name"), str)
                            for row in classifications)):
                        raise PlatformError("OS pool classifications are unavailable")
                    if not any(row["name"] == "Power Pool Alpha" for row in classifications):
                        ids.append(_component(item["id"], "Alpha ID"))
                pools[region_key] = ids
            comparators = [other for other in pools[region_key] if other != alpha_id]
            if not comparators:
                result["reason"] = "no_eligible_os_comparators"
                return result

            target = self._get_pnl(alpha_id, cache)
        except PlatformError:
            result["reason"] = "local_self_evidence_unavailable"
            return result
        for other in comparators:
            try:
                other_pnl = self._get_pnl(other, cache)
                evidence = pairwise_pnl_correlation(target, other_pnl, window_years=4)
                if evidence is None:
                    raise PlatformError("local self PnL comparison is unavailable")
                evidence["alpha_id"] = other
                evidence["alpha_pnl"] = self._save_pnl_evidence(alpha_id, target)
                evidence["other_pnl"] = self._save_pnl_evidence(other, other_pnl)
                result["comparisons"].append(evidence)
            except (PlatformError, OSError, TypeError, ValueError):
                result["missing"].append(other)
        values = [abs(row["correlation"]) for row in result["comparisons"]]
        result["comparison_count"] = len(values)
        result["value"] = max(values) if values else None
        result["complete"] = bool(values) and not result["missing"]
        if result["value"] is not None and result["value"] > limit:
            result["status"] = "fail"
            result["reason"] = "local_self_limit_exceeded"
        elif result["complete"]:
            result["status"] = "pass"
            result["reason"] = "all_local_self_comparisons_complete"
        else:
            result["reason"] = "local_self_comparisons_incomplete"
        return result

    def _correlation_data(self, alpha_id, endpoint):
        for attempt in range(5):
            response = self._request("GET", f"/alphas/{alpha_id}/correlations/{endpoint}")
            retry_after = response.headers.get("Retry-After")
            try:
                data = self._json(response)
            except PlatformError:
                if retry_after is None and response.status_code != 202:
                    raise
                data = None
            pending = response.status_code == 202 or (retry_after is not None and not data)
            if not pending:
                return data
            if attempt < 4:
                try:
                    delay = max(0.1, min(30.0, float(retry_after or 2)))
                except (TypeError, ValueError):
                    delay = 2.0
                time.sleep(delay)
        raise PlatformError("platform correlation is still pending")

    def _correlations(self, alpha_id, contender_ids=None, *, cache=None):
        if contender_ids is None:
            contender_ids = []
        if not isinstance(contender_ids, list):
            raise PlatformError("case contenders must be an explicit list")
        others = list(dict.fromkeys(_component(value, "Alpha ID") for value in contender_ids
                                    if value != alpha_id))
        details = self._recent_alpha_details.get(alpha_id) or {}
        is_data = details.get("is") if isinstance(details, dict) else None
        platform_checks = is_data.get("checks") if isinstance(is_data, dict) else None
        checks = {}
        for name, endpoint, check_name in (("production", "prod", "PROD_CORRELATION"),
                                           ("self", "self", "SELF_CORRELATION")):
            check = terminal_correlation_check(platform_checks, check_name)
            if check is not None:
                checks[name] = {"platform_check": check}
                continue
            try:
                data = self._correlation_data(alpha_id, endpoint)
                checks[name] = {"correlation_data": data}
            except PlatformError as exc:
                checks[name] = {"correlation_data": None, "error": type(exc).__name__}
        pairs, unavailable = {}, []
        if others:
            try:
                target = self._get_pnl(alpha_id, cache)
            except PlatformError:
                target = None
            for other in others:
                if target is None:
                    unavailable.append(other)
                    continue
                try:
                    other_pnl = self._get_pnl(other, cache)
                    evidence = pairwise_pnl_correlation(target, other_pnl)
                    if evidence is None:
                        raise PlatformError("case PnL correlation is unavailable")
                    evidence["alpha_pnl"] = self._save_pnl_evidence(alpha_id, target)
                    evidence["other_pnl"] = self._save_pnl_evidence(other, other_pnl)
                    pairs[other] = evidence
                except (PlatformError, OSError, TypeError, ValueError):
                    unavailable.append(other)
        return {"alpha_id": alpha_id, "checks": checks,
                "case_pairwise": pairs, "case_pairwise_unavailable": unavailable}

    def audit_alphas(self, params):
        """Read back current evidence and bind it to this project's local receipt."""
        paths = self._batch_paths(params.get("batch_id"), "production",
                                  params.get("case_key"), params.get("run_id"))
        ids, entries = params.get("alpha_ids"), params.get("entries")
        contenders = params.get("contender_ids", ids)
        if (not isinstance(ids, list) or not ids or len(set(ids)) != len(ids)
                or not isinstance(entries, dict) or not isinstance(contenders, list)):
            raise PlatformError("audit needs explicit Alpha IDs and local evidence")
        for alpha_id in ids + contenders:
            _component(alpha_id, "Alpha ID")
        same_batch = params.get("batch_id") == self._prefilter_batch
        self._local_self_results = {}
        self._recent_alpha_details = {}
        if not same_batch:
            self._prefilter_batch = None
            self._prefilter_cache = {}
        request, receipt = self._load(paths["request"]), self._load(paths["receipt"])
        progress, saved = self._load(paths["progress"]), self._load(paths["output"])
        bound = (isinstance(request, dict) and isinstance(receipt, dict)
                 and isinstance(progress, dict) and isinstance(saved, list)
                 and progress.get("status") == "COMPLETE"
                 and progress.get("request_sha256") == hashlib.sha256(paths["request"].read_bytes()).hexdigest()
                 and progress.get("receipt_sha256") == hashlib.sha256(paths["receipt"].read_bytes()).hexdigest()
                 and progress.get("output_sha256") == hashlib.sha256(paths["output"].read_bytes()).hexdigest()
                 and receipt.get("dataset") == request.get("dataset_id")
                 and receipt.get("variants") == request.get("variants")
                 and isinstance(request.get("settings"), dict)
                 and receipt.get("settings") == self._platform_settings(request.get("settings", {})))
        variants = receipt.get("variants") if isinstance(receipt, dict) else None
        admissions = receipt.get("field_admissions") if isinstance(receipt, dict) else None
        results = []
        local_correlation_cache = {}
        if same_batch:
            self._prefilter_cache = local_correlation_cache
        for alpha_id in ids:
            details = self._json(self._request("GET", "/alphas/" + alpha_id))
            pending, failures = [], []
            entry = entries.get(alpha_id)
            admission = None
            try:
                if not bound or not isinstance(entry, dict):
                    raise ValueError("missing evidence")
                if (Path(entry["receipt"]).resolve() != paths["receipt"].resolve()
                        or Path(entry["results"]).resolve() != paths["output"].resolve()
                        or entry.get("dataset") != receipt.get("dataset")):
                    raise ValueError("incorrect receipt location")
                indices = [i for i, row in enumerate(variants)
                           if isinstance(row, dict) and row.get("name") == entry.get("variant")]
                matching = [row for row in saved if isinstance(row, dict) and row.get("alpha_id") == alpha_id]
                if len(indices) != 1 or len(matching) != 1:
                    raise ValueError("ambiguous Alpha binding")
                index = indices[0]
                variant = variants[index]
                admission = admissions[index]
                if (matching[0].get("expression") != variant.get("expression")
                        or matching[0].get("name") != variant.get("name")
                        or not isinstance(admission, dict) or admission.get("passed") is not True):
                    raise ValueError("field or expression binding mismatch")
                created = datetime.fromisoformat(receipt["created_at"].replace("Z", "+00:00"))
                simulated = datetime.fromisoformat(details["dateCreated"].replace("Z", "+00:00"))
                if created.tzinfo is None or simulated.tzinfo is None or created > simulated:
                    raise ValueError("receipt is not pre-backtest")
            except (OSError, KeyError, TypeError, IndexError, ValueError, AttributeError):
                pending.append("pre_backtest_evidence_unverified")
                admission = None
            if not isinstance(details, dict) or details.get("id") != alpha_id:
                raise PlatformError("audit Alpha readback is unavailable")
            is_data = details.get("is")
            if not isinstance(is_data, dict):
                risk_state = "unreadable"
                pending.append("risk_neutralized_unavailable")
            elif "riskNeutralized" not in is_data:
                risk_state = "absent"
            elif not isinstance(is_data["riskNeutralized"], dict):
                risk_state = "unreadable"
                pending.append("risk_neutralized_unavailable")
            else:
                risk_state = "present"
                for metric in ("sharpe", "fitness", "margin"):
                    value = is_data["riskNeutralized"].get(metric)
                    try:
                        readable = not isinstance(value, bool) and math.isfinite(float(value))
                    except (TypeError, ValueError, OverflowError):
                        readable = False
                    if not readable:
                        pending.append("risk_neutralized_" + metric + "_unavailable")
            ra_state = ra_correlation_check_state(details)
            eligible = ra_state == "pass"
            if ra_state != "pass":
                pending.append("ra_checks_unsettled_or_unavailable")
            others = [other for other in contenders if other != alpha_id]
            raw_correlations = {}
            local_self = None
            if eligible:
                try:
                    local_self = self._local_self_correlation(
                        alpha_id, params.get("self_corr_max"), cache=local_correlation_cache)
                    if same_batch:
                        self._local_self_results[alpha_id] = local_self
                    latest_details = local_self.get("ra_details")
                    if isinstance(latest_details, dict) and latest_details != details:
                        details = latest_details
                        pending.append("ra_changed_during_prefilter")
                    if local_self.get("ra_state") == "fail":
                        failures.append("ra_failed_count")
                    local_state = local_self_correlation_state(local_self, params.get("self_corr_max"))
                    if local_state == "pass":
                        raw_correlations = self._correlations(alpha_id, others, cache=local_correlation_cache)
                    elif local_state == "fail":
                        pending.append("platform_correlation_skipped_after_local_self_failure")
                    else:
                        pending.append("local_self_correlation_unavailable")
                except PlatformError:
                    pending.append("local_self_correlation_unavailable")
            correlations = parse_correlations(raw_correlations, others)
            if correlations.get("prod") is None or correlations.get("self") is None:
                pending.append("individual_correlation_unavailable")
            pairs = []
            for other in others:
                value = correlations.get("pairwise", {}).get(other)
                if value is None:
                    pending.append("pairwise_correlation_unavailable")
                else:
                    pairs.append({"pair": [alpha_id, other], "corr": value, "verdict": "measured"})
            reviews = entry.get("reviews") if isinstance(entry, dict) else None
            for kind in ("robustness", "tradeoff"):
                review = reviews.get(kind) if isinstance(reviews, dict) else None
                if (not isinstance(review, dict) or review.get("status") not in {"clear", "resolved"}
                        or not isinstance(review.get("evidence"), str) or not review["evidence"].strip()):
                    pending.append(kind + "_review")
            regular = details.get("regular")
            expression = regular.get("code") if isinstance(regular, dict) else None
            pending = sorted(set(pending))
            results.append({"alpha_id": alpha_id, "raw_details": details,
                "expression": expression, "field_admission": admission,
                "risk_neutralized_state": risk_state,
                "local_self_correlation": local_self,
                "correlation_raw": raw_correlations,
                "prod_corr": correlations.get("prod"), "self_corr": correlations.get("self"),
                "pairwise": pairs, "pairwise_state": ("not_applicable" if not others else
                                                        "evaluated" if pairs else "unavailable"),
                "candidate_metric_failures": [], "gate_failures": failures, "gate_pending": pending,
                "audit_kind": "local_evidence", "evidence_complete": not failures and not pending,
                "candidate_status": "evidence_complete" if not failures and not pending else "checks_pending"})
        audit_id = _component(params.get("audit_id") or params["batch_id"], "audit ID")
        audit_path = self.case_root / "audits" / f"{audit_id}.json"
        atomic_write_json(audit_path, results)
        return {"results": results, "audit_path": str(audit_path)}

    def dispatch(self, command, params):
        if command == "check_simulation_cache":
            return self.check_simulation_cache(params)
        if command == "sync_backtest_cache":
            return self.sync_backtest_cache()
        if command == "tag_alpha":
            return self.tag_alpha(params)
        if command == "set_case":
            return self.set_case(params.get("case_key"), params.get("run_id"))
        if command == "validate_simulation_settings":
            return self.validate_simulation_settings(params.get("settings"))
        if command in {"get_datasets", "get_datafields", "get_operators"}:
            with SingleWriterLock(self._snapshot_root / ".catalog.lock"):
                return self._catalog(command, params)
        if command == "get_platform_setting_options":
            return self._setting_options()
        if command == "get_simulation_settings_schema":
            return self._settings_schema()
        if command == "get_dataset_details":
            return self._json(self._request("GET", "/data-sets/" + _component(params.get("dataset_id"), "Dataset ID")))
        if command == "simulate_batch":
            return self.run_simulation_batch(params)
        if command == "simulate_super":
            return self.run_super_simulation(params)
        if command == "resume_simulation_batch":
            return self.resume_simulation_batch(params)
        if command == "check_local_self_correlation":
            alpha_id = _component(params.get("alpha_id"), "Alpha ID")
            batch_id = params.get("batch_id")
            cache = None
            if batch_id is not None:
                batch_id = _component(batch_id, "batch ID")
                if batch_id != self._prefilter_batch:
                    self._prefilter_batch = batch_id
                    self._prefilter_cache = {}
                    self._local_self_results = {}
                    self._recent_alpha_details = {}
                cache = self._prefilter_cache
            result = self._local_self_correlation(alpha_id, params.get("self_corr_max"), cache=cache)
            self._local_self_results[alpha_id] = result
            return result
        if command == "check_correlation":
            alpha_id = _component(params.get("alpha_id"), "Alpha ID")
            if (params.get("batch_id") is not None and
                    _component(params["batch_id"], "batch ID") != self._prefilter_batch):
                raise PlatformError("platform correlation belongs to a different batch")
            # Independent gateway-side ordering guard: refresh details and
            # require all readable RA checks to be terminal PASS before PC/SC.
            details = self._json(self._request("GET", f"/alphas/{alpha_id}"))
            if not isinstance(details, dict) or details.get("id") != alpha_id:
                raise PlatformError("Alpha details are unavailable for correlation")
            if ra_correlation_check_state(details) != "pass":
                raise PlatformError("PC/SC correlation is forbidden until RA checks all PASS")
            local = self._local_self_results.get(alpha_id)
            if local_self_correlation_state(local, params.get("self_corr_max")) != "pass":
                raise PlatformError(
                    "PC/SC correlation is forbidden until local self-correlation is complete and PASS")
            self._recent_alpha_details[alpha_id] = details
            cache = self._prefilter_cache if params.get("batch_id") is not None else None
            return self._correlations(alpha_id, params.get("contender_ids"), cache=cache)
        if command == "get_user_alphas":
            allowed = {key: params[key] for key in ("stage", "limit", "offset", "order") if key in params}
            return self._json(self._request("GET", "/users/self/alphas", params=allowed))
        if command in {"get_alpha_details", "get_alpha_pnl", "get_alpha_yearly_stats"}:
            alpha = _component(params.get("alpha_id"), "Alpha ID")
            suffix = {"get_alpha_details": "", "get_alpha_pnl": "/recordsets/pnl",
                      "get_alpha_yearly_stats": "/recordsets/yearly-stats"}[command]
            return self._json(self._request("GET", f"/alphas/{alpha}{suffix}"))
        if command == "audit_alphas":
            return self.audit_alphas(params)
        raise PlatformError(f"unsupported platform command: {command}")
