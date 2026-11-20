"""Shared integrity envelope for persistent research and model-call state.

Both durable research checkpoints and model-attempt ledgers store the same
schema-independent state_sha256 over strict canonical JSON. This is an
accidental-corruption detector, NOT a signature or protection against
someone with filesystem write access.
"""
import hmac

from alpha_research.state.fingerprint import fingerprint_json
from alpha_research.state.store import atomic_write_json


CHECKSUM_KEY = "state_sha256"


def state_payload(state):
    return {key: value for key, value in state.items() if key != CHECKSUM_KEY}


def seal_state(state):
    """Seal a mutable state mapping in place and return it."""
    if not isinstance(state, dict):
        raise ValueError("only JSON object state may be sealed")
    state[CHECKSUM_KEY] = fingerprint_json(state_payload(state))
    return state


def is_sealed_state(state):
    """Reject legacy, truncated, malformed, or mutated state."""
    if not isinstance(state, dict):
        return False
    expected = state.get(CHECKSUM_KEY)
    if not isinstance(expected, str) or len(expected) != 64:
        return False
    try:
        actual = fingerprint_json(state_payload(state))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, actual)


def write_sealed_json(path, state):
    """Persist only a sealed object using the common atomic JSON writer."""
    atomic_write_json(path, seal_state(state))
