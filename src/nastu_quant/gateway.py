"""Gateway (C3, AGENT_DESIGN.md §4): the single client of the driver
process. Enforces the command whitelist client-side and the RA-Gate
precondition for correlation checks before anything reaches the driver.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from .gatekeeper import assert_correlation_allowed
from .paths import expand_path

WHITELIST = {
    "get_platform_setting_options",
    "get_simulation_settings_schema",
    "get_datasets", "get_dataset_details", "get_datafields", "get_operators",
    "set_case", "validate_simulation_settings", "simulate_batch",
    "resume_simulation_batch", "audit_alphas",
    "get_alpha_details", "get_alpha_pnl", "get_alpha_yearly_stats",
    "check_correlation", "get_user_alphas",
}
READ_ONLY_COMMANDS = {
    "get_platform_setting_options", "get_simulation_settings_schema",
    "get_datasets", "get_dataset_details", "get_datafields", "get_operators",
    "validate_simulation_settings", "get_alpha_details", "get_alpha_pnl",
    "get_alpha_yearly_stats", "check_correlation", "get_user_alphas",
    "audit_alphas",
}


class GatewayError(RuntimeError):
    pass


class Gateway:
    def __init__(self, driver_path: Path = None, alpha_lookup=None):
        """alpha_lookup: callable(alpha_id) -> alpha state dict, used for
        the correlation precondition check (design §3.3, second layer)."""
        self.driver_path = driver_path or (Path(__file__).with_name("driver.py"))
        self.alpha_lookup = alpha_lookup
        self.proc = None
        self.auth_info = None

    def start(self) -> dict:
        interpreter = os.environ.get("BRAIN_PYTHON")
        if not interpreter:
            raise GatewayError("BRAIN_PYTHON must point to the brain-flow interpreter")
        interpreter_path = expand_path(interpreter).resolve()
        if not interpreter_path.is_absolute() or not interpreter_path.is_file():
            raise GatewayError("BRAIN_PYTHON must identify an existing absolute interpreter")
        source_root = Path(__file__).resolve().parents[1]
        brain_flow_home = os.environ.get("BRAIN_FLOW_HOME")
        python_path = [str(source_root)]
        if brain_flow_home:
            python_path.insert(0, str(expand_path(brain_flow_home).resolve() / "scripts"))
        if os.environ.get("PYTHONPATH"):
            python_path.append(os.environ["PYTHONPATH"])
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(python_path)
        self.proc = subprocess.Popen(
            [str(interpreter_path), str(self.driver_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=env)
        self.auth_info = self._read_response()
        if not self.auth_info.get("ok"):
            raise GatewayError(f"brain-flow runtime unavailable: {self.auth_info}")
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

    def call(self, cmd: str, **params) -> dict:
        if cmd not in WHITELIST:
            raise GatewayError(f"command not in whitelist: {cmd}")
        if cmd == "check_correlation":
            if self.alpha_lookup is None:
                raise GatewayError("correlation check refused: alpha state lookup unavailable")
            alpha_id = params.get("alpha_id") or params.get("alphaId")
            alpha = self.alpha_lookup(alpha_id) if alpha_id else None
            if alpha is None:
                raise GatewayError(
                    "correlation check refused: alpha state unknown")
            assert_correlation_allowed(alpha)  # raises GateOrderError
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
