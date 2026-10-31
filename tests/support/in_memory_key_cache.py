"""KeyCache fakes for tests: a dict-backed one, and one that is always down."""

from typing import NoReturn

from llm_gateway.tenancy.model import Tenant
from llm_gateway.tenancy.ports import REVOKED, KeyCacheUnavailableError, Revoked


class InMemoryKeyCache:
    """Same rules as Redis, minus expiry: remember never overwrites, a revoke mark always does."""

    def __init__(self) -> None:
        self._entries: dict[str, Tenant | Revoked] = {}

    async def tenant_for_key(self, key_hash: str) -> Tenant | Revoked | None:
        return self._entries.get(key_hash)

    async def remember(self, key_hash: str, tenant: Tenant) -> None:
        self._entries.setdefault(key_hash, tenant)

    async def mark_revoked(self, key_hash: str) -> None:
        self._entries[key_hash] = REVOKED


class UnreachableKeyCache:
    """Stands in for Redis being down: every call fails the way the real adapter reports it."""

    def __init__(self) -> None:
        self.calls = 0

    async def tenant_for_key(self, key_hash: str) -> Tenant | Revoked | None:
        self._fail()

    async def remember(self, key_hash: str, tenant: Tenant) -> None:
        self._fail()

    async def mark_revoked(self, key_hash: str) -> None:
        self._fail()

    def _fail(self) -> NoReturn:
        self.calls += 1
        raise KeyCacheUnavailableError("key cache is down")
