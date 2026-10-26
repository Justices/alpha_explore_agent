"""Gateway (C3, AGENT_DESIGN.md §4): the single client of the driver
process. Enforces the command whitelist client-side and the RA-Gate
precondition for correlation checks before anything reaches the driver.
"""
import json
import subprocess
import sys
from pathlib import Path

from .gatekeeper import assert_correlation_allowed

WHITELIST = {
    "get_datasets", "get_datafields", "get_operators",
    "create_simulation", "create_multi_simulation",
    "get_alpha_details", "get_alpha_pnl", "get_alpha_yearly_stats",
    "check_correlation", "get_submission_check", "get_user_alphas",
    "set_alpha_properties",
}
INTERNAL_COMMANDS = {"reauth", "shutdown"}


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
        self.proc = subprocess.Popen(
            [sys.executable, str(self.driver_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, bufsize=1)
        self.auth_info = self._read_response()
        if not self.auth_info.get("ok"):
            raise GatewayError(f"driver auth failed: {self.auth_info}")
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
        if cmd == "check_correlation" and self.alpha_lookup is not None:
            alpha_id = params.get("alpha_id") or params.get("alphaId")
            alpha = self.alpha_lookup(alpha_id) if alpha_id else None
            if alpha is None:
                raise GatewayError(
                    "correlation check refused: alpha state unknown")
            assert_correlation_allowed(alpha)  # raises GateOrderError
        resp = self._send({"cmd": cmd, **params})
        if not resp.get("ok"):
            raise GatewayError(f"{cmd} failed: {resp.get('error')}")
        return resp.get("result", resp)

    def reauth(self) -> dict:
        """Ask the driver to re-run the FR-01 cascade's level 2
        (it re-reads the credential file itself; no secrets pass here)."""
        resp = self._send({"cmd": "reauth"})
        if not resp.get("ok"):
            raise GatewayError(f"reauth failed: {resp.get('error')}")
        return resp

    def shutdown(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self._send({"cmd": "shutdown"})
            finally:
                self.proc.wait(timeout=10)
