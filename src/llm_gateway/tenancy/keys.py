import hashlib
import re
import secrets
from dataclasses import dataclass

# gw_ + 8 hex prefix + the 43 characters token_urlsafe(32) produces.
_KEY_SHAPE = re.compile(r"gw_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")


@dataclass(frozen=True)
class IssuedKey:
    plaintext: str
    prefix: str
    key_hash: str


def hash_key(plaintext: str) -> str:
    """Stored instead of the key, so a DB leak exposes no usable keys."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def issue_key() -> IssuedKey:
    """Mint a new key: gw_<prefix>_<secret>, plus its hash for storage."""
    prefix = secrets.token_hex(4)
    secret = secrets.token_urlsafe(32)
    plaintext = f"gw_{prefix}_{secret}"
    return IssuedKey(plaintext=plaintext, prefix=prefix, key_hash=hash_key(plaintext))


def looks_like_key(candidate: str) -> bool:
    """Cheap shape check, so garbage tokens are refused without a cache or database lookup."""
    return _KEY_SHAPE.fullmatch(candidate) is not None
