"""KeyCache fakes for tests: a dict-backed one, and one that is always down."""

from llm_gateway.tenancy.model import Tenant
from llm_gateway.tenancy.ports import KeyCacheUnavailableError


class InMemoryKeyCache:
    def __init__(self) -> None:
        self._tenants_by_hash: dict[str, Tenant] = {}

    async def tenant_for_key(self, key_hash: str) -> Tenant | None:
        return self._tenants_by_hash.get(key_hash)

    async def remember(self, key_hash: str, tenant: Tenant) -> None:
        self._tenants_by_hash[key_hash] = tenant

    async def forget(self, key_hash: str) -> None:
        self._tenants_by_hash.pop(key_hash, None)


class UnreachableKeyCache:
    """Stands in for Redis being down: every call fails the way the real adapter reports it."""

    async def tenant_for_key(self, key_hash: str) -> Tenant | None:
        raise KeyCacheUnavailableError("key cache is down")

    async def remember(self, key_hash: str, tenant: Tenant) -> None:
        raise KeyCacheUnavailableError("key cache is down")

    async def forget(self, key_hash: str) -> None:
        raise KeyCacheUnavailableError("key cache is down")
