"""CLI-owned active research context; never constructs or starts providers.

The existing CLI remains responsible for credentials, Gateway.start(), the
single process-wide OS lock, model selection and the one ModelCallLedger
per domain Research ID. This object only transfers references after those
operations succeed; it is neither an authorization nor a second session.
"""
from dataclasses import dataclass
from pathlib import Path

from alpha_research.orchestration.engine import OrchestrationError
from alpha_research.orchestration.mock_workspace import (
    marker_path, inspect_mock_workspace,
)


@dataclass(frozen=True)
class CliRuntimeContext:
    state_root: Path
    config: dict
    gateway: object
    model: object
    agent: object
    execution_lock_path: Path

    @classmethod
    def from_active_cli(cls, root, config, gateway, model, agent, lock):
        root = Path(root).resolve()
        expected = root / "state" / "agent.lock"
        if (lock is None or Path(getattr(lock, "path", "")).resolve() != expected
                or getattr(lock, "handle", None) is None):
            raise OrchestrationError(
                "CLI runtime requires the already-acquired global execution lock")
        if gateway is None or not isinstance(config, dict):
            raise OrchestrationError("active CLI Gateway/config unavailable")
        marker = marker_path(root)
        if marker.exists() or marker.is_symlink():
            inspect_mock_workspace(root)
            if (not getattr(gateway, "is_offline_mock", False)
                    or not getattr(model, "is_offline_mock", False)):
                raise OrchestrationError(
                    "active live CLI runtime forbidden in offline mock workspace")
        return cls(root, config, gateway, model, agent, expected)

    def dispatch(self, command, args, *, dispatcher=None, execution_context=None):
        """Reuse the original domain Dispatch with existing CLI objects."""
        if dispatcher is None:
            from alpha_research.orchestration.dispatch import dispatch as dispatcher

        if execution_context is None:
            return dispatcher(
                command, args, self.config, self.state_root,
                self.gateway, self.agent)
        return dispatcher(
            command, args, self.config, self.state_root,
            self.gateway, self.agent, execution_context=execution_context)
