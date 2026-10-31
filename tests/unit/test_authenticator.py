from uuid import UUID

import pytest

from llm_gateway.tenancy.auth import Authenticator, InvalidApiKeyError
from llm_gateway.tenancy.keys import IssuedKey, issue_key
from llm_gateway.tenancy.model import StoredKey, Tenant
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
async def test_malformed_key_is_rejected(
    store: InMemoryTenantStore, cache: InMemoryKeyCache, presented: str
) -> None:
    with pytest.raises(InvalidApiKeyError, match="malformed"):
        await Authenticator(store, cache).authenticate(presented)


async def test_unknown_key_is_rejected(store: InMemoryTenantStore, cache: InMemoryKeyCache) -> None:
    with pytest.raises(InvalidApiKeyError, match="unknown or revoked"):
        await Authenticator(store, cache).authenticate(issue_key().plaintext)


async def test_revoked_key_is_rejected(store: InMemoryTenantStore, cache: InMemoryKeyCache) -> None:
    issued = await a_key_for(store, ACME)
    await store.revoke_key(issued.prefix)

    with pytest.raises(InvalidApiKeyError):
        await Authenticator(store, cache).authenticate(issued.plaintext)


async def test_cache_down_falls_back_to_the_store(store: InMemoryTenantStore) -> None:
    issued = await a_key_for(store, ACME)

    tenant = await Authenticator(store, UnreachableKeyCache()).authenticate(issued.plaintext)

    assert tenant == ACME
