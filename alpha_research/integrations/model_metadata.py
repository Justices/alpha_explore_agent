"""Invocation-local model metadata for synchronous Agent/API adapters.

Four last_* fields are retained for the existing complete_json interface,
but are no longer shared across concurrent threads or asyncio contexts.
"""
import copy
from contextvars import ContextVar


_FIELDS = ("last_usage", "last_model", "last_response_text", "last_thread_id")


class InvocationMetadata:
    def _init_invocation_metadata(self):
        self._invocation_metadata = ContextVar(
            f"invocation_metadata_{id(self)}", default=None)
        self._reset_invocation_metadata()

    def _reset_invocation_metadata(self):
        self._invocation_metadata.set({name: None for name in _FIELDS})

    def _metadata(self, name):
        snapshot = self._invocation_metadata.get()
        if snapshot is None:
            return None
        return copy.deepcopy(snapshot.get(name))

    def _set_metadata(self, name, value):
        # Copy-on-write is needed: a ContextVar value may be inherited by
        # multiple asynchronous contexts; mutation must stay local.
        snapshot = dict(self._invocation_metadata.get() or {})
        snapshot[name] = copy.deepcopy(value)
        self._invocation_metadata.set(snapshot)

    def _capture_metadata(self, backend):
        self._invocation_metadata.set({
            name: copy.deepcopy(getattr(backend, name, None))
            for name in _FIELDS
        })

    @property
    def last_usage(self):
        return self._metadata("last_usage")

    @last_usage.setter
    def last_usage(self, value):
        self._set_metadata("last_usage", value)

    @property
    def last_model(self):
        return self._metadata("last_model")

    @last_model.setter
    def last_model(self, value):
        self._set_metadata("last_model", value)

    @property
    def last_response_text(self):
        return self._metadata("last_response_text")

    @last_response_text.setter
    def last_response_text(self, value):
        self._set_metadata("last_response_text", value)

    @property
    def last_thread_id(self):
        return self._metadata("last_thread_id")

    @last_thread_id.setter
    def last_thread_id(self, value):
        self._set_metadata("last_thread_id", value)
