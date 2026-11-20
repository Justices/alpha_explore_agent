"""Configurable research-role router compatible with DecisionRecorder.

The application chooses a task; roles choose capabilities; backends implement
model transport. The router itself never invokes a BRAIN tool or simulation.
"""
import copy
from threading import Lock

from alpha_research.agents.configuration import AgentConfigError, RoleRoutingConfig
from alpha_research.agents.antigravity import AntigravityModelClient
from alpha_research.integrations.model_metadata import InvocationMetadata


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


class RoleRoutedModelClient(InvocationMetadata):
    """Implements complete_json(task, context) and config_for(task).

    Each backend is lazily initialized. All result types remain JSON objects.
    Existing research workflows and DecisionRecorder need no interface changes.
    """
    def __init__(self, config, *, backend_factory=None, environ=None):
        self.routing = RoleRoutingConfig.parse(config)
        self.backend_factory = backend_factory
        self.environ = environ
        self.instances = {}
        self._instance_locks = {}
        self._factory_lock = Lock()
        # Invocation receipt is scoped to this thread/async context.
        self._init_invocation_metadata()

    def _make_backend(self, binding):
        if self.backend_factory is not None:
            return self.backend_factory(binding.backend)
        provider = binding.backend.get("provider")
        if provider == "mock":
            return MockModelClient(binding.backend)
        if provider == "antigravity_sdk":
            return AntigravityModelClient(binding.backend, environ=self.environ)
        # Lazy import avoids a circular dependency with the provider adapters.
        from alpha_research.integrations.model import ConfiguredModelClient
        return ConfiguredModelClient(binding.backend, environ=self.environ)

    def route_for(self, task):
        """One validated snapshot of role, backend identity and provider spec."""
        return self.routing.resolve(task)

    def _backend(self, task):
        binding = self.route_for(task)
        # Lazy construction and lock creation must be one transaction.
        with self._factory_lock:
            if binding.backend_name not in self.instances:
                self.instances[binding.backend_name] = self._make_backend(binding)
                self._instance_locks[binding.backend_name] = Lock()
            return (binding, self.instances[binding.backend_name],
                    self._instance_locks[binding.backend_name])

    def config_for(self, task):
        return self.route_for(task).backend

    def role_for(self, task):
        return self.route_for(task).role

    def backend_name_for(self, task):
        return self.route_for(task).backend_name

    def complete_json(self, task, context, *, thread_path=None):
        # Clear this invocation's metadata before route resolution. An invalid
        # task cannot inherit the last successful call's usage or model name.
        self._reset_invocation_metadata()
        _, backend, call_lock = self._backend(task)
        with call_lock:
            try:
                return backend.complete_json(task, context, thread_path=thread_path)
            finally:
                # Snapshot even on provider errors: DecisionRecorder needs the
                # error response text/usage from this exact failed invocation.
                # Do not copy backend references into the invocation context.
                self._capture_metadata(backend)
