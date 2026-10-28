"""Adapter for the canonical brain-flow scripts and shared read-only session."""

import asyncio
import hashlib
import importlib
import inspect
import json
import os
import re
import subprocess
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from .paths import expand_path
from .state_store import atomic_write_json


class BrainFlowError(RuntimeError):
    pass


class BrainFlowRuntime:
    """Route supported work through brain-flow without owning BRAIN auth."""

    READ_ONLY_FUNCTIONS = {
        "get_platform_setting_options", "get_alpha_details", "get_alpha_pnl",
        "get_alpha_yearly_stats", "check_correlation", "get_user_alphas",
    }
    READ_ONLY_COMMANDS = READ_ONLY_FUNCTIONS | {
        "get_simulation_settings_schema", "get_dataset_details", "audit_alphas",
    }
    SCRIPT_COMMANDS = {"get_datasets", "get_datafields", "get_operators"}
    BATCH_LIMIT = 8

    def __init__(self, brain_flow_home=None, brain_python=None):
        home = brain_flow_home or os.environ.get("BRAIN_FLOW_HOME")
        python = brain_python or os.environ.get("BRAIN_PYTHON")
        if not home:
            raise BrainFlowError("Set BRAIN_FLOW_HOME to the brain-flow repository")
        if not python:
            raise BrainFlowError("Set BRAIN_PYTHON to the absolute brain-flow interpreter path")
        self.home = expand_path(home).resolve()
        self.scripts = self.home / "scripts"
        self.python = expand_path(python).resolve()
        if not self.scripts.is_dir():
            raise BrainFlowError(f"brain-flow scripts directory is unavailable: {self.scripts}")
        if not self.python.is_absolute() or not self.python.is_file():
            raise BrainFlowError("BRAIN_PYTHON must identify an existing absolute interpreter")
        self.runs_root = (self.home / "runs").resolve()
        self.case_key = None
        self.case_root = None
        self.run_id = None
        self.run_root = None
        self._session = None
        self._client = None
        self._loop = asyncio.new_event_loop()

    def close(self):
        if self._loop.is_running():
            return
        self._loop.close()

    def set_case(self, case_key, run_id=None):
        if not isinstance(case_key, str):
            raise BrainFlowError("brain-flow case key must be a relative string")
        parts = tuple(part for part in re.split(r"[/\\]+", case_key) if part)
        if len(parts) != 1 or any(part in {".", ".."} or
                                 not re.fullmatch(r"[A-Za-z0-9_-]+", part)
                                 for part in parts):
            raise BrainFlowError("brain-flow case key must be one safe path component")
        target = (self.runs_root.joinpath(*parts)).resolve()
        if not target.is_relative_to(self.runs_root):
            raise BrainFlowError("brain-flow case directory escaped runs/")
        target.mkdir(parents=True, exist_ok=True)
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise BrainFlowError("brain-flow run ID must be a safe path component")
        run_root = target / "sessions" / run_id
        run_root.mkdir(parents=True, exist_ok=True)
        self.case_key = "/".join(parts)
        self.case_root = target
        self.run_id = run_id
        self.run_root = run_root
        return {"case_key": self.case_key, "run_id": run_id,
                "case_root": str(target), "run_root": str(run_root)}

    def dispatch(self, command, params):
        if command == "set_case":
            return self.set_case(params.get("case_key"), params.get("run_id"))
        if command == "validate_simulation_settings":
            return self.validate_simulation_settings(params.get("settings"))
        if command in self.SCRIPT_COMMANDS:
            return self._script_catalog_call(command, params)
        if command == "simulate_batch":
            return self.run_simulation_batch(params)
        if command == "resume_simulation_batch":
            return self.resume_simulation_batch(params)
        if command == "audit_alphas":
            return self.audit_alphas(params)
        if command in self.READ_ONLY_COMMANDS:
            return self._read_only_call(command, params)
        raise BrainFlowError(f"unsupported brain-flow adapter command: {command}")

    def _require_case(self):
        if (self.case_root is None or self.case_key is None
                or self.run_root is None or self.run_id is None):
            raise BrainFlowError("set a research case and run before calling brain-flow scripts")
        return self.run_root

    def _script_process(self, name, args):
        script = self.scripts / name
        if not script.is_file():
            raise BrainFlowError(f"brain-flow entry point is unavailable: {script}")
        command = [str(self.python), str(script), *map(str, args)]
        try:
            result = subprocess.run(
                command, cwd=str(self.home), capture_output=True, text=True,
                encoding="utf-8", errors="replace", check=False)
        except OSError as exc:
            raise BrainFlowError(f"could not start brain-flow/{name}: {exc}") from exc
        return result

    def _script(self, name, args):
        result = self._script_process(name, args)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()[-4000:]
            raise BrainFlowError(
                f"brain-flow/{name} exited {result.returncode}: {detail or 'no diagnostic'}")
        return result

    def audit_alphas(self, params):
        case_key = params.get("case_key") or self.case_key
        run_id = params.get("run_id") or self.run_id
        self.set_case(case_key, run_id)
        case_root = self.case_root
        batch_id = params.get("batch_id")
        alpha_ids = params.get("alpha_ids")
        entries = params.get("entries")
        if (not isinstance(batch_id, str) or
                not re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{8}", batch_id)):
            raise BrainFlowError("audit_alphas requires a valid local batch ID")
        if (not isinstance(alpha_ids, list) or not alpha_ids
                or any(not isinstance(alpha_id, str) or
                       not re.fullmatch(r"[A-Za-z0-9_-]+", alpha_id)
                       for alpha_id in alpha_ids)):
            raise BrainFlowError("audit_alphas requires explicit Alpha IDs")
        if not isinstance(entries, dict):
            raise BrainFlowError("audit_alphas requires case-local evidence entries")
        review_path = case_root / "reviews" / f"{batch_id}.json"
        out_path = case_root / "audits" / f"{batch_id}.json"
        atomic_write_json(review_path, {
            "case_key": self.case_key, "review_set": alpha_ids,
            "entries": entries,
        })
        result = self._script_process("audit_alphas.py", [
            "--case-spec", review_path, "--out", out_path, "--no-tag"])
        if not out_path.is_file():
            detail = (result.stderr or result.stdout or "").strip()[-4000:]
            raise BrainFlowError(
                f"audit_alphas.py exited {result.returncode} without saved evidence: "
                f"{detail or 'no diagnostic'}")
        try:
            rows = json.loads(out_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BrainFlowError("audit_alphas.py saved unreadable evidence") from exc
        if not isinstance(rows, list):
            raise BrainFlowError("audit_alphas.py result must be a list")
        return {"results": rows, "case_spec": str(review_path),
                "audit_path": str(out_path), "return_code": result.returncode}

    def _script_catalog_call(self, command, params):
        case_root = self._require_case()
        region = str(params.get("region") or "").upper()
        universe = str(params.get("universe") or "").upper()
        delay = params.get("delay")
        if (not re.fullmatch(r"[A-Z0-9_-]+", region)
                or not re.fullmatch(r"[A-Z0-9_-]+", universe)
                or delay not in (0, 1)):
            raise BrainFlowError(f"{command} requires an exact Region/Delay/Universe")

        if command == "get_datasets":
            output_dir = case_root / "datasets"
            self._script("fetch_datasets.py", [
                "--instrument-type", str(params.get("instrument_type") or "EQUITY"),
                "--region", region, "--delay", delay, "--universe", universe,
                "--refresh", "--limit", 1000000, "--output-dir", output_dir,
            ])
            raw_path = output_dir / "raw" / (
                f"brain_{region.lower()}_equity_d{delay}_{universe.lower()}_datasets_raw.json")
            try:
                snapshot = json.loads(raw_path.read_text(encoding="utf-8"))
                metadata = snapshot.get("metadata") or {}
                raw = snapshot.get("raw_payload") or {}
                rows = raw.get("results")
            except (OSError, ValueError) as exc:
                raise BrainFlowError("Dataset script did not leave a readable raw snapshot") from exc
            if (metadata.get("region") != region or metadata.get("delay") != delay
                    or metadata.get("universe") != universe or not isinstance(rows, list)):
                raise BrainFlowError("Dataset script snapshot does not confirm the requested scope")
            return rows

        if command == "get_datafields":
            dataset_id = params.get("dataset_id")
            if not isinstance(dataset_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", dataset_id):
                raise BrainFlowError("get_datafields requires a safe Dataset ID")
            cache_root = case_root / "fields"
            screening_path = case_root / "field_screening" / f"{dataset_id}.json"
            self._script("inspect_fields.py", [
                "--region", region, "--delay", delay, "--universe", universe,
                "--dataset", dataset_id, "--refresh", "--cache-root", cache_root,
                "--out", screening_path,
            ])
            try:
                result = json.loads(screening_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise BrainFlowError("Field script did not leave a readable screening snapshot") from exc
            metadata = result.get("metadata") or {}
            fields = result.get("fields")
            if (metadata.get("region") != region or metadata.get("delay") != delay
                    or metadata.get("universe") != universe
                    or metadata.get("status") != "complete" or not isinstance(fields, list)):
                raise BrainFlowError("Field script snapshot is incomplete or out of scope")
            return fields

        output_dir = case_root / "operators"
        self._script("fetch_brain_operators.py", ["--output-dir", output_dir])
        operator_path = output_dir / "processed" / "brain_operators.json"
        try:
            result = json.loads(operator_path.read_text(encoding="utf-8"))
            operators = result.get("operators")
        except (OSError, ValueError) as exc:
            raise BrainFlowError("Operator script did not leave a readable catalog") from exc
        if not isinstance(operators, list) or not operators:
            raise BrainFlowError("Operator script returned an empty or invalid catalog")
        return operators

    def _load_session(self):
        if self._session is not None:
            return self._session
        scripts_path = str(self.scripts)
        if scripts_path not in sys.path:
            sys.path.insert(0, scripts_path)
        try:
            self._session = importlib.import_module("brain_session")
        except Exception as exc:
            raise BrainFlowError(f"brain_session.py could not be imported: {exc}") from exc
        return self._session

    def _get_client(self):
        session = self._load_session()
        self._client = self._loop.run_until_complete(
            session.ensure_authenticated(self._client))
        return self._client, session.get_platform_module()

    def _read_only_call(self, command, params):
        if command not in self.READ_ONLY_COMMANDS:
            raise BrainFlowError(f"read-only operation not allowed: {command}")
        client, platform = self._get_client()

        if command == "get_simulation_settings_schema":
            response = client.session.options(f"{client.base_url}/simulations", timeout=40)
            response.raise_for_status()
            return response.json()["actions"]["POST"]["settings"]["children"]

        if command == "get_dataset_details":
            dataset_id = params.get("dataset_id")
            if not isinstance(dataset_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", dataset_id):
                raise BrainFlowError("get_dataset_details requires a safe Dataset ID")
            response = client.session.get(
                f"{client.base_url}/data-sets/{dataset_id}", timeout=40)
            response.raise_for_status()
            return response.json()

        function = getattr(platform, command, None)
        if function is None:
            raise BrainFlowError(f"brain-flow read-only fallback is unavailable: {command}")
        try:
            with redirect_stdout(StringIO()):
                result = function(**params)
                if inspect.isawaitable(result):
                    result = self._loop.run_until_complete(result)
            return result
        except Exception as exc:
            message = str(exc)
            if "401" not in message and "unauthorized" not in message.lower():
                raise
            # Only bounded read-only calls are retried after the shared owner
            # revalidates its cookie/credential scope.
            self._client = self._loop.run_until_complete(
                self._load_session().ensure_authenticated(self._client))
            with redirect_stdout(StringIO()):
                result = function(**params)
                if inspect.isawaitable(result):
                    result = self._loop.run_until_complete(result)
            return result

    @staticmethod
    def validate_simulation_settings(settings):
        if not isinstance(settings, dict):
            raise BrainFlowError("effective simulation settings are required")
        unsupported = []
        if settings.get("instrument_type", "EQUITY") != "EQUITY":
            unsupported.append("instrument_type")
        pasteurization = settings.get("pasteurization")
        if not isinstance(pasteurization, str) or not pasteurization.strip():
            unsupported.append("pasteurization (an explicit value is required)")
        test_period = settings.get("test_period")
        if test_period is not None and (not isinstance(test_period, str) or not test_period.strip()):
            unsupported.append("test_period (must be a nonempty string)")
        for key in ("start_date", "end_date", "testPeriod", "max_position"):
            if settings.get(key) is not None:
                unsupported.append(f"{key} (no simulate_batch.py CLI option)")
        if settings.get("visualization") not in (None, False):
            unsupported.append("visualization (simulate_batch.py fixes it to false)")
        if unsupported:
            raise BrainFlowError(
                "brain-flow simulate_batch.py cannot preserve the effective settings: "
                + ", ".join(unsupported))
        required = ("region", "universe", "delay", "decay", "neutralization",
                    "truncation", "unit_handling", "nan_handling", "max_trade", "language")
        missing = [key for key in required if settings.get(key) is None]
        if missing:
            raise BrainFlowError(f"effective simulation settings are incomplete: {missing}")
        return {"compatible": True, "entry_point": "scripts/simulate_batch.py"}

    @staticmethod
    def _batch_label(value):
        if not isinstance(value, str) or not re.fullmatch(r"(?:production|probe_[1-9][0-9]*)", value):
            raise BrainFlowError("invalid simulation batch label")
        return value

    def _batch_paths(self, batch_id, label, case_key=None, run_id=None):
        if case_key is not None or run_id is not None:
            self.set_case(case_key or self.case_key, run_id or self.run_id)
        run_root = self._require_case()
        if not isinstance(batch_id, str) or not re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{8}", batch_id):
            raise BrainFlowError("invalid local batch ID")
        label = self._batch_label(label)
        folder = run_root / "batches"
        stem = f"{batch_id}_{label}"
        return {
            "input": folder / f"{stem}.input.json",
            "request": folder / f"{stem}.request.json",
            "output": folder / f"{stem}.json",
            "progress": folder / f"{stem}.progress.json",
            "log": folder / f"{stem}.log",
        }

    @staticmethod
    def _load_result(path):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BrainFlowError(f"canonical simulation result is unreadable: {path}") from exc
        if not isinstance(result, list) or any(not isinstance(row, dict) for row in result):
            raise BrainFlowError("canonical simulation result must be a list of rows")
        return result

    def _batch_cli_args(self, request, paths, resume_url=None):
        settings = request["settings"]
        self.validate_simulation_settings(settings)
        args = [
            "--file", paths["input"], "--dataset", request["dataset_id"],
            "--field-cache-root", self._require_case() / "fields",
            "--region", settings["region"], "--universe", settings["universe"],
            "--delay", settings["delay"], "--decay", settings["decay"],
            "--neutralization", settings["neutralization"],
            "--truncation", settings["truncation"],
            "--unit-handling", settings["unit_handling"],
            "--nan-handling", settings["nan_handling"],
            "--max-trade", settings["max_trade"],
            "--language", settings["language"], "--out", paths["output"],
        ]
        if resume_url:
            args.extend(["--resume", resume_url])
        return args

    def run_simulation_batch(self, params):
        paths = self._batch_paths(params.get("batch_id"), params.get("label"))
        request = {
            "case_key": self.case_key,
            "run_id": self.run_id,
            "batch_id": params["batch_id"], "label": params["label"],
            "dataset_id": params.get("dataset_id"),
            "settings": params.get("settings"),
            "variants": params.get("variants"),
        }
        if paths["output"].exists():
            return {"results": self._load_result(paths["output"]),
                    "runner": self._runner_metadata(paths)}
        if paths["progress"].exists():
            prior = json.loads(paths["progress"].read_text(encoding="utf-8"))
            if prior.get("resume_url"):
                raise BrainFlowError("a submitted batch exists; call resume_simulation_batch")
            raise BrainFlowError("prior runner state is unresolved; reconcile before retrying")
        self.validate_simulation_settings(request["settings"])
        dataset_id = request["dataset_id"]
        variants = request["variants"]
        if (not isinstance(dataset_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", dataset_id)
                or not isinstance(variants, list) or not 1 <= len(variants) <= self.BATCH_LIMIT):
            raise BrainFlowError("simulation batch requires one to eight variants and one Dataset")
        if any(not isinstance(row, dict) or row.get("dataset") not in (None, dataset_id)
               for row in variants):
            raise BrainFlowError("simulation batch cannot mix Datasets")
        variant_settings = {"pasteurization": request["settings"]["pasteurization"]}
        if request["settings"].get("test_period") is not None:
            variant_settings["testPeriod"] = request["settings"]["test_period"]
        input_payload = [
            {"name": row.get("name"), "expression": row.get("expression"),
             "dataset": dataset_id, "research": row.get("research"),
             "settings": dict(variant_settings)}
            for row in variants
        ]
        paths["input"].parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(paths["input"], input_payload)
        atomic_write_json(paths["request"], request)
        return self._execute_batch(request, paths)

    def resume_simulation_batch(self, params):
        paths = self._batch_paths(params.get("batch_id"), params.get("label"),
                                  case_key=params.get("case_key"),
                                  run_id=params.get("run_id"))
        if paths["output"].exists():
            return {"results": self._load_result(paths["output"]),
                    "runner": self._runner_metadata(paths)}
        try:
            request = json.loads(paths["request"].read_text(encoding="utf-8"))
            progress = json.loads(paths["progress"].read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BrainFlowError("canonical runner recovery evidence is unavailable") from exc
        if (request.get("case_key") != self.case_key
                or request.get("run_id") != self.run_id):
            raise BrainFlowError("canonical runner recovery belongs to a different research case")
        resume_url = progress.get("resume_url")
        if not isinstance(resume_url, str) or not resume_url.startswith("https://"):
            raise BrainFlowError("canonical runner has no persisted simulation URL; reconcile read-only")
        return self._execute_batch(request, paths, resume_url=resume_url)

    def _execute_batch(self, request, paths, resume_url=None):
        if resume_url is None and paths["progress"].exists():
            raise BrainFlowError("runner state exists without an explicit recovery URL; no repost")
        args = self._batch_cli_args(request, paths, resume_url=resume_url)
        command = [str(self.python), str(self.scripts / "simulate_batch.py"), *map(str, args)]
        progress = {"status": "RUNNING", "case_key": self.case_key,
                    "batch_id": request["batch_id"], "label": request["label"],
                    "resume_url": resume_url, "input_sha256": hashlib.sha256(
                        paths["input"].read_bytes()).hexdigest()}
        atomic_write_json(paths["progress"], progress)
        tail = ""
        url_pattern = re.compile(
            r"https://api\.worldquantbrain\.com/simulations/[A-Za-z0-9_./-]+")
        try:
            with paths["log"].open("a", encoding="utf-8", newline="\n") as log:
                process = subprocess.Popen(
                    command, cwd=str(self.home), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    errors="replace", bufsize=1)
                if process.stdout is None:
                    raise BrainFlowError("simulation runner stdout was unavailable")
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    tail = (tail + line)[-4000:]
                    found = url_pattern.search(line)
                    if found:
                        progress.update({"status": "SUBMITTED", "resume_url": found.group(0).rstrip(".,);]")})
                        atomic_write_json(paths["progress"], progress)
                return_code = process.wait()
        except OSError as exc:
            progress.update({"status": "START_FAILED", "error": str(exc)[:1000]})
            atomic_write_json(paths["progress"], progress)
            raise BrainFlowError(f"could not start simulate_batch.py: {exc}") from exc

        if paths["output"].exists():
            rows = self._load_result(paths["output"])
            progress.update({"status": "COMPLETE" if return_code == 0 else "UNRESOLVED",
                             "return_code": return_code})
            atomic_write_json(paths["progress"], progress)
            return {"results": rows, "runner": self._runner_metadata(paths)}
        progress.update({"status": "UNRESOLVED", "return_code": return_code,
                         "tail": tail[-2000:]})
        atomic_write_json(paths["progress"], progress)
        raise BrainFlowError(
            f"simulate_batch.py returned {return_code} without a result file; "
            "inspect its persisted progress and reconcile before any retry")

    def _runner_metadata(self, paths):
        progress = {}
        request = {}
        try:
            progress = json.loads(paths["progress"].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        try:
            request = json.loads(paths["request"].read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        return {"case_key": request.get("case_key"),
                "run_id": request.get("run_id"),
                "field_cache_root": str(paths["input"].parent.parent / "fields"),
                "input": str(paths["input"]), "output": str(paths["output"]),
                "progress": str(paths["progress"]), "log": str(paths["log"]),
                "resume_url": progress.get("resume_url"), "status": progress.get("status")}
