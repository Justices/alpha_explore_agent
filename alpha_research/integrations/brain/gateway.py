"""Gateway (C3, AGENT_DESIGN.md §4): the single client of the driver
process. Enforces the command whitelist client-side and the RA-Gate
precondition for correlation checks before anything reaches the driver.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from alpha_research.qualification.gates import (
    assert_correlation_allowed, local_self_correlation_state, ra_correlation_check_state,
)
from alpha_research.integrations.brain.guarded_protocol import (
    GUARDED_ORCHESTRATION_READS, GUARDED_FORBIDDEN_CLAIMS,
    READONLY_PROBE_COMMANDS,
    guarded_request_allowed,
)
from alpha_research.orchestration.research_catalog_protocol import (
    research_catalog_request_allowed,
)

WHITELIST = {
    "check_simulation_cache",
    "readonly_probe_datasets",
    "sync_backtest_cache",
    "tag_alpha",
    "get_platform_setting_options",
    "get_simulation_settings_schema",
    "get_datasets", "get_dataset_details", "get_datafields", "get_operators",
    "set_case", "validate_simulation_settings", "simulate_batch", "simulate_super",
    "resume_simulation_batch", "audit_alphas", "check_local_self_correlation",
    "get_alpha_details", "get_alpha_pnl", "get_alpha_yearly_stats",
    "check_correlation", "get_user_alphas",
}
READ_ONLY_COMMANDS = {
    "check_simulation_cache",
    "get_platform_setting_options", "get_simulation_settings_schema",
    "get_datasets", "get_dataset_details", "get_datafields", "get_operators",
    "validate_simulation_settings", "get_alpha_details", "get_alpha_pnl",
    "check_local_self_correlation",
    "get_alpha_yearly_stats", "check_correlation", "get_user_alphas",
    "audit_alphas",
}


# Strict probe actions: no state-changing research commands, even if the
# broader Gateway whitelist grows over time.
# Orchestration production mode defaults to a minimal *positive* read
# allowlist. Future commands cannot silently become authorized writes.
class GatewayError(RuntimeError):
    pass


class Gateway:
    def __init__(self, driver_path: Path = None, alpha_lookup=None, *,
                 credentials=None, read_only=False,
                 orchestration_guarded=False,
                 research_catalog_scope=None,
                 research_catalog_source_alpha_id=None):
        """alpha_lookup: callable(alpha_id) -> alpha state dict, used for
        the correlation precondition check (design §3.3, second layer)."""
        if type(read_only) is not bool or type(orchestration_guarded) is not bool:
            raise GatewayError("Gateway execution modes require explicit booleans")
        self.driver_path = driver_path or (Path(__file__).with_name("driver.py"))
        self.alpha_lookup = alpha_lookup
        self._audit_evidence = {}
        self._credentials = credentials
        self.read_only = bool(read_only)
        # New orchestration integration MUST use this boundary. Existing
        # legacy Gateway callers remain independent of the control plane.
        # No trusted live admission consumer exists yet: guarded mutating
        # commands are always refused, even if a caller fabricates params.
        self.orchestration_guarded = bool(orchestration_guarded)
        # Exact research-only profile is separate from legacy Gateway and
        # the narrow read_only probe. It is NOT a grant or live enablement.
        self.research_catalog_scope = research_catalog_scope
        self.research_catalog_source_alpha_id = research_catalog_source_alpha_id
        if research_catalog_scope is not None:
            if read_only or orchestration_guarded:
                raise GatewayError("research catalog profile cannot combine Gateway modes")
            if not research_catalog_request_allowed(
                    "get_datasets", research_catalog_scope,
                    research_catalog_scope):
                raise GatewayError("research catalog requires exact pinned scope")
        elif research_catalog_source_alpha_id is not None:
            raise GatewayError("research catalog source Alpha needs pinned scope")
        self.proc = None
        self.auth_info = None

    def start(self) -> dict:
        if self.research_catalog_scope is not None:
            # No independent remote consumption/session attestation exists.
            # A local key file or claim cannot permit subprocess startup.
            raise GatewayError(
                "research Catalog requires independent grant consumer; live disabled")
        env = os.environ.copy()
        command = [sys.executable, str(self.driver_path)]
        if self.read_only:
            command.append("--read-only")
        if self.orchestration_guarded:
            command.append("--orchestration-guarded")
        if self._credentials is not None:
            command.append("--brain-credentials-stdin")
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env)
        if self._credentials is not None:
            self.proc.stdin.write(json.dumps(self._credentials) + "\n")
            self.proc.stdin.flush()
            self._credentials = None
        self.auth_info = self._read_response()
        if not self.auth_info.get("ok"):
            # Driver startup responses may contain provider details. Do not
            # echo raw authentication payloads into CLI errors or audit logs.
            raise GatewayError("platform runtime unavailable; authentication or session initialization failed")
        return self.auth_info

    def _read_response(self) -> dict:
        line = self.proc.stdout.readline()
        if not line:
            raise GatewayError("driver closed stdout unexpectedly")
        return json.loads(line)

    def _send(self, payload: dict) -> dict:
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        return self._read_response()

    def set_audit_evidence(self, details, self_corr_max, local_self=None):
        alpha_id = details.get("id")
        if not isinstance(alpha_id, str) or not alpha_id or ra_correlation_check_state(details) != "pass":
            raise GatewayError("terminal RA evidence is required for the audit")
        self._audit_evidence[alpha_id] = {
            "state": "RA_PASSED", "ra_readable": True, "ra_fail_count": 0,
            "self_corr_max": self_corr_max, "local_self": local_self}

    def clear_audit_evidence(self, alpha_id):
        self._audit_evidence.pop(alpha_id, None)

    def check_simulation_cache(self, **params):
        return self.call("check_simulation_cache", **params)

    def call(self, cmd: str, **params) -> dict:
        if self.research_catalog_scope is not None:
            if not research_catalog_request_allowed(
                    cmd, params, self.research_catalog_scope,
                    source_alpha_id=self.research_catalog_source_alpha_id):
                raise GatewayError(
                    "research Catalog command or scope is not admitted")
            raise GatewayError(
                "research Catalog requires independent grant consumer; live disabled")
        if self.orchestration_guarded and cmd not in GUARDED_ORCHESTRATION_READS:
            # Negative admission is independently rechecked by the driver.
            raise GatewayError(
                "orchestrated BRAIN write requires trusted live admission "
                "consumer; dispatch disabled")
        if self.orchestration_guarded and not guarded_request_allowed(cmd, params):
            raise GatewayError(
                "orchestrated BRAIN read refuses untrusted execution claims")
        if self.read_only and cmd not in READONLY_PROBE_COMMANDS:
            raise GatewayError("read-only BRAIN probe forbids this command")
        if cmd not in WHITELIST:
            raise GatewayError(f"command not in whitelist: {cmd}")
        if cmd in {"check_correlation", "check_local_self_correlation"}:
            alpha_id = params.get("alpha_id") or params.get("alphaId")
            alpha = self._audit_evidence.get(alpha_id)
            if alpha is None and self.alpha_lookup is not None and alpha_id:
                alpha = self.alpha_lookup(alpha_id)
            if alpha is None:
                raise GatewayError(
                    "correlation check refused: alpha state unknown")
            assert_correlation_allowed(alpha)  # raises GateOrderError
            if (cmd == "check_correlation" and
                    local_self_correlation_state(alpha.get("local_self"),
                                                 alpha.get("self_corr_max")) != "pass"):
                raise GatewayError("platform correlation requires a completed local self prefilter")
        if self.proc is None or self.proc.poll() is not None:
            raise GatewayError("driver is not running")
        resp = self._send({"cmd": cmd, **params})
        if not resp.get("ok"):
            # Never propagate untrusted provider error bodies: they may
            # contain credentials, request headers or account identifiers.
            raise GatewayError(f"{cmd} failed in platform driver")
        return resp.get("result", resp)

    def shutdown(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self._send({"cmd": "shutdown"})
            finally:
                self.proc.wait(timeout=10)
