"""Resolve model API credentials without storing them in the model registry.

Environment variables take precedence over a private JSON file. The file is
optional and is never copied into model routing, journals, or diagnostic output.
"""
import json
import os
from pathlib import Path


def credential_file_for(state_root):
    return Path(state_root).expanduser().resolve() / "config" / "model_credentials.json"


def resolve_model_credential(env_name, *, environ=None, credential_file=None):
    """Return a secret or None; never include secret material in errors."""
    environment = os.environ if environ is None else environ
    secret = environment.get(env_name)
    if isinstance(secret, str) and secret.strip():
        return secret
    if credential_file is None:
        return None
    path = Path(credential_file)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError("model credentials file is unreadable or invalid") from None
    if not isinstance(data, dict) or set(data) != {"credentials"} or not isinstance(data["credentials"], dict):
        raise ValueError("model credentials file must contain a credentials object")
    value = data["credentials"].get(env_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("model credential entry must be a nonempty string")
    return value
