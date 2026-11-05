"""KeyCache fakes for tests: a dict-backed one, and one that is always down."""

from typing import NoReturn

from llm_gateway.tenancy.model import Tenant
from llm_gateway.tenancy.ports import REVOKED, CachedTenant, KeyCacheUnavailableError, Revoked


class InMemoryKeyCache:
    """Same rules as Redis, minus expiry: remember never overwrites, a revoke mark always
    does, refresh never replaces a mark. Entries stay fresh until a test calls go_stale()."""

    def __init__(self) -> None:
        self._entries: dict[str, CachedTenant | Revoked] = {}

    async def tenant_for_key(self, key_hash: str) -> CachedTenant | Revoked | None:
        return self._entries.get(key_hash)

    async def remember(self, key_hash: str, tenant: Tenant) -> None:
        self._entries.setdefault(key_hash, CachedTenant(tenant, fresh=True))

    async def refresh(self, key_hash: str, tenant: Tenant) -> None:
        if isinstance(self._entries.get(key_hash), CachedTenant):
            self._entries[key_hash] = CachedTenant(tenant, fresh=True)

    async def mark_revoked(self, key_hash: str) -> None:
        self._entries[key_hash] = REVOKED

    def go_stale(self, key_hash: str) -> None:
        """As if the fresh window had passed since the key was last confirmed."""
        entry = self._entries[key_hash]
        assert isinstance(entry, CachedTenant)
        self._entries[key_hash] = CachedTenant(entry.tenant, fresh=False)


class UnreachableKeyCache:
    """Stands in for Redis being down: every call fails the way the real adapter reports it."""

    def __init__(self) -> None:
        self.calls = 0

    async def tenant_for_key(self, key_hash: str) -> CachedTenant | Revoked | None:
        self._fail()

    async def remember(self, key_hash: str, tenant: Tenant) -> None:
        self._fail()

    async def refresh(self, key_hash: str, tenant: Tenant) -> None:
        self._fail()

    async def mark_revoked(self, key_hash: str) -> None:
        self._fail()

    def _fail(self) -> NoReturn:
        self.calls += 1
        raise KeyCacheUnavailableError("key cache is down")
