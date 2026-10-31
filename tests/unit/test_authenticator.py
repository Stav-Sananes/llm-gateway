from collections.abc import Awaitable, Callable
from uuid import UUID

import pytest

from llm_gateway.tenancy.admin import TenantAdmin
from llm_gateway.tenancy.auth import Authenticator, InvalidApiKeyError
from llm_gateway.tenancy.keys import IssuedKey, issue_key
from llm_gateway.tenancy.model import StoredKey, Tenant
from llm_gateway.tenancy.ports import TenantStoreUnavailableError
from tests.support.in_memory_key_cache import InMemoryKeyCache, UnreachableKeyCache
from tests.support.in_memory_tenant_store import InMemoryTenantStore

pytestmark = pytest.mark.anyio

ACME = Tenant(id=UUID(int=1), name="acme")


@pytest.fixture
def store() -> InMemoryTenantStore:
    return InMemoryTenantStore()


@pytest.fixture
def cache() -> InMemoryKeyCache:
    return InMemoryKeyCache()


async def a_key_for(store: InMemoryTenantStore, tenant: Tenant) -> IssuedKey:
    await store.add_tenant(tenant)
    issued = issue_key()
    await store.add_key(
        StoredKey(tenant_id=tenant.id, prefix=issued.prefix, key_hash=issued.key_hash)
    )
    return issued


async def test_valid_key_authenticates_as_its_tenant(
    store: InMemoryTenantStore, cache: InMemoryKeyCache
) -> None:
    issued = await a_key_for(store, ACME)

    assert await Authenticator(store, cache).authenticate(issued.plaintext) == ACME


async def test_first_lookup_fills_the_cache(
    store: InMemoryTenantStore, cache: InMemoryKeyCache
) -> None:
    issued = await a_key_for(store, ACME)

    await Authenticator(store, cache).authenticate(issued.plaintext)

    assert await cache.tenant_for_key(issued.key_hash) == ACME


async def test_cached_key_authenticates_without_the_store(cache: InMemoryKeyCache) -> None:
    issued = issue_key()
    await cache.remember(issued.key_hash, ACME)
    empty_store = InMemoryTenantStore()

    assert await Authenticator(empty_store, cache).authenticate(issued.plaintext) == ACME


@pytest.mark.parametrize("presented", ["", "sk-not-ours", "gw_deadbeef_short"])
async def test_malformed_key_is_rejected_before_any_lookup(presented: str) -> None:
    # Both backends are down: reaching either would raise an outage error instead.
    cache = UnreachableKeyCache()

    with pytest.raises(InvalidApiKeyError):
        await Authenticator(UnreachableStore(), cache).authenticate(presented)

    assert cache.calls == 0


async def test_unknown_key_is_rejected(store: InMemoryTenantStore, cache: InMemoryKeyCache) -> None:
    with pytest.raises(InvalidApiKeyError):
        await Authenticator(store, cache).authenticate(issue_key().plaintext)


async def test_revoked_key_is_rejected(store: InMemoryTenantStore, cache: InMemoryKeyCache) -> None:
    issued = await a_key_for(store, ACME)
    await store.revoke_key(issued.prefix)

    with pytest.raises(InvalidApiKeyError):
        await Authenticator(store, cache).authenticate(issued.plaintext)


async def test_key_marked_revoked_in_the_cache_is_rejected(
    store: InMemoryTenantStore, cache: InMemoryKeyCache
) -> None:
    issued = await a_key_for(store, ACME)  # still active in the store
    await cache.mark_revoked(issued.key_hash)

    with pytest.raises(InvalidApiKeyError):
        await Authenticator(store, cache).authenticate(issued.plaintext)


async def test_revoke_during_a_lookup_does_not_put_the_key_back_in_the_cache(
    store: InMemoryTenantStore, cache: InMemoryKeyCache
) -> None:
    issued = await a_key_for(store, ACME)
    admin = TenantAdmin(store, cache)
    revoking_mid_lookup = RevokesAfterRead(store, lambda: admin.revoke_key(issued.prefix))

    # The in-flight request read the key before the revoke, so it may still be served...
    await Authenticator(revoking_mid_lookup, cache).authenticate(issued.plaintext)

    # ...but no later request may be.
    with pytest.raises(InvalidApiKeyError):
        await Authenticator(store, cache).authenticate(issued.plaintext)


async def test_cache_down_falls_back_to_the_store(store: InMemoryTenantStore) -> None:
    issued = await a_key_for(store, ACME)

    tenant = await Authenticator(store, UnreachableKeyCache()).authenticate(issued.plaintext)

    assert tenant == ACME


async def test_cache_down_costs_one_cache_call_not_two(store: InMemoryTenantStore) -> None:
    issued = await a_key_for(store, ACME)
    cache = UnreachableKeyCache()

    await Authenticator(store, cache).authenticate(issued.plaintext)

    assert cache.calls == 1  # the failed lookup; no write that would wait out a second timeout


class UnreachableStore(InMemoryTenantStore):
    async def tenant_for_key(self, key_hash: str) -> Tenant | None:
        raise TenantStoreUnavailableError("store is down")


class RevokesAfterRead(InMemoryTenantStore):
    """Reads the real store, then lets a revoke land before the caller caches the answer."""

    def __init__(self, store: InMemoryTenantStore, revoke: Callable[[], Awaitable[None]]) -> None:
        super().__init__()
        self._store = store
        self._revoke = revoke

    async def tenant_for_key(self, key_hash: str) -> Tenant | None:
        tenant = await self._store.tenant_for_key(key_hash)
        await self._revoke()
        return tenant
