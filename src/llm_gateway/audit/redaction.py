"""Redaction runs before hashing: secrets never enter the chain, so they never need deleting."""

import re
from collections.abc import Iterable, Mapping
from typing import Any

REDACTED = "[REDACTED]"

# Keys whose values are always secret, matched case-insensitively as substrings.
_SECRET_KEYS = ("password", "secret", "token", "api_key", "apikey", "authorization", "cookie")
# Values that look like credentials wherever they appear.
_SECRET_VALUES = re.compile(
    r"gw_[0-9a-f]{8}_[A-Za-z0-9_-]+"  # our own API keys
    r"|sk-ant-[A-Za-z0-9_-]+"  # Anthropic keys
    r"|sk-[A-Za-z0-9]{20,}"  # OpenAI-style keys
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
)


def redact(value: Any, extra_keys: Iterable[str] = ()) -> Any:
    """A copy of `value` with secret-looking keys and values replaced by REDACTED."""
    secret_keys = tuple(_SECRET_KEYS) + tuple(key.lower() for key in extra_keys)
    return _redact(value, secret_keys)


def _redact(value: Any, secret_keys: tuple[str, ...]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): REDACTED if _is_secret_key(str(key), secret_keys) else _redact(v, secret_keys)
            for key, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact(item, secret_keys) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUES.sub(REDACTED, value)
    return value


def _is_secret_key(key: str, secret_keys: tuple[str, ...]) -> bool:
    lowered = key.lower()
    return any(secret in lowered for secret in secret_keys)
