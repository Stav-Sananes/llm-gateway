"""Redaction runs before hashing: secrets never enter the chain, so they never need deleting."""

import re
from collections.abc import Iterable, Mapping
from typing import Any

from llm_gateway.audit.chain import UnauditableEventError

REDACTED = "[REDACTED]"

# A key is secret when, with separators removed and lowercased, it ends with one of these:
# `api_key`, `x-api-key`, `access_token`, `DB_PASSWORD`, `sessionToken` are secret;
# `max_tokens`, `input_tokens`, `token_count` are not (they're usage numbers).
_SECRET_KEY_ENDINGS = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "authorization",
    "cookie",
    "credentials",
    "privatekey",
)
# Values that look like credentials wherever they appear.
_SECRET_VALUES = re.compile(
    r"gw_[0-9a-f]{8}_[A-Za-z0-9_-]+"  # our own API keys
    r"|sk-ant-[A-Za-z0-9_-]+"  # Anthropic keys
    r"|sk-[A-Za-z0-9_-]{20,}"  # OpenAI keys (sk-…, sk-proj-…)
    r"|Bearer\s+[A-Za-z0-9._~+/=-]+"
    r"|eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"  # JWTs
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
)


def redact(value: Any, extra_keys: Iterable[str] = ()) -> Any:
    """A copy of `value` with secret-looking keys and values replaced by REDACTED.

    Raises UnauditableEventError on a non-string key: `{1: …, "1": …}` would otherwise
    collapse into one key and two different events would look the same.
    """
    endings = _SECRET_KEY_ENDINGS + tuple(_normalized(key) for key in extra_keys)
    return _redact(value, endings)


def _redact(value: Any, endings: tuple[str, ...]) -> Any:
    if isinstance(value, Mapping):
        return {
            _key(k): REDACTED if _is_secret(k, endings) else _redact(v, endings)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_redact(item, endings) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUES.sub(REDACTED, value)
    return value


def _key(key: object) -> str:
    if not isinstance(key, str):
        raise UnauditableEventError(f"audit details need string keys, got {key!r}")
    return key


def _is_secret(key: str, endings: tuple[str, ...]) -> bool:
    return _normalized(key).endswith(endings)


def _normalized(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())
