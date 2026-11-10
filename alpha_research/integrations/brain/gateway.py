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

WHITELIST = {
    "check_simulation_cache",
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


class GatewayError(RuntimeError):
    pass


class Gateway:
    def __init__(self, driver_path: Path = None, alpha_lookup=None, *, credentials=None):
        """alpha_lookup: callable(alpha_id) -> alpha state dict, used for
        the correlation precondition check (design §3.3, second layer)."""
        self.driver_path = driver_path or (Path(__file__).with_name("driver.py"))
        self.alpha_lookup = alpha_lookup
        self._audit_evidence = {}
        self._credentials = credentials
        self.proc = None
        self.auth_info = None

    def start(self) -> dict:
        env = os.environ.copy()
        command = [sys.executable, str(self.driver_path)]
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
            raise GatewayError(f"platform runtime unavailable: {self.auth_info}")
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
            raise GatewayError(f"{cmd} failed: {resp.get('error')}")
        return resp.get("result", resp)

    def shutdown(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self._send({"cmd": "shutdown"})
            finally:
                self.proc.wait(timeout=10)
