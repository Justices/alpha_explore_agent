"""Configurable research-role router compatible with DecisionRecorder.

The application chooses a task; roles choose capabilities; backends implement
model transport. The router itself never invokes a BRAIN tool or simulation.
"""
import copy

from alpha_research.agents.configuration import AgentConfigError, RoleRoutingConfig
from alpha_research.agents.antigravity import AntigravityModelClient


class MockModelClient:
    """Deterministic offline provider; no network, subprocesses or external actions."""
    def __init__(self, config):
        self.config = dict(config)
        self.last_usage = None
        self.last_model = "offline-mock"
        self.last_response_text = None
        self.last_thread_id = None

    def complete_json(self, task, context, *, thread_path=None):
        responses = self.config.get("responses", {})
        if task not in responses:
            raise AgentConfigError(f"missing offline mock response for {task}")
        answer = responses[task]
        if isinstance(answer, list):
            index = context.get("round_index", 0) if isinstance(context, dict) else 0
            if type(index) is not int or not 0 <= index < len(answer):
                raise AgentConfigError(f"mock response exhausted for {task}")
            answer = answer[index]
        if not isinstance(answer, dict):
            raise AgentConfigError("offline mock response must be a JSON object")
        return copy.deepcopy(answer)


class RoleRoutedModelClient:
    """Implements complete_json(task, context) and config_for(task).

    Each backend is lazily initialized. All result types remain JSON objects.
    Existing research workflows and DecisionRecorder need no interface changes.
    """
    def __init__(self, config, *, backend_factory=None, environ=None):
        self.routing = RoleRoutingConfig.parse(config)
        self.backend_factory = backend_factory
        self.environ = environ
        self.instances = {}
        self.active = None
        self.active_binding = None

    def _make_backend(self, binding):
        if self.backend_factory is not None:
            return self.backend_factory(binding.backend)
        provider = binding.backend.get("provider")
        if provider == "mock":
            return MockModelClient(binding.backend)
        if provider == "antigravity_sdk":
            return AntigravityModelClient(binding.backend, environ=self.environ)
        # Late import avoids a circular dependency with the legacy-compatible
        # ConfiguredModelClient and keeps the older provider contracts intact.
        from alpha_research.integrations.model import ConfiguredModelClient
        return ConfiguredModelClient(binding.backend, environ=self.environ)

    def _backend(self, task):
        binding = self.routing.resolve(task)
        if binding.backend_name not in self.instances:
            self.instances[binding.backend_name] = self._make_backend(binding)
        return binding, self.instances[binding.backend_name]

    def config_for(self, task):
        return self.routing.resolve(task).backend

    def role_for(self, task):
        return self.routing.resolve(task).role

    def backend_name_for(self, task):
        return self.routing.resolve(task).backend_name

    @property
    def last_usage(self):
        return getattr(self.active, "last_usage", None)

    @property
    def last_model(self):
        return getattr(self.active, "last_model", None)

    @property
    def last_response_text(self):
        return getattr(self.active, "last_response_text", None)

    @property
    def last_thread_id(self):
        return getattr(self.active, "last_thread_id", None)

    def complete_json(self, task, context, *, thread_path=None):
        binding, backend = self._backend(task)
        self.active = backend
        self.active_binding = binding
        return backend.complete_json(task, context, thread_path=thread_path)
