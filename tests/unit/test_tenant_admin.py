import pytest

from llm_gateway.tenancy.admin import KeyCacheNotClearedError, TenantAdmin
from llm_gateway.tenancy.keys import hash_key
from llm_gateway.tenancy.ports import DuplicateTenantError, KeyNotFoundError, TenantNotFoundError
from tests.support.in_memory_key_cache import InMemoryKeyCache, UnreachableKeyCache
from tests.support.in_memory_tenant_store import InMemoryTenantStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def store() -> InMemoryTenantStore:
    return InMemoryTenantStore()


@pytest.fixture
def cache() -> InMemoryKeyCache:
    return InMemoryKeyCache()


@pytest.fixture
def admin(store: InMemoryTenantStore, cache: InMemoryKeyCache) -> TenantAdmin:
    return TenantAdmin(store, cache)


async def test_created_tenant_is_saved(store: InMemoryTenantStore, admin: TenantAdmin) -> None:
    tenant = await admin.create_tenant("acme")

    assert await store.tenant_named("acme") == tenant


async def test_each_tenant_gets_its_own_id(admin: TenantAdmin) -> None:
    acme = await admin.create_tenant("acme")
    globex = await admin.create_tenant("globex")

    assert acme.id != globex.id


async def test_tenant_names_are_unique(admin: TenantAdmin) -> None:
    await admin.create_tenant("acme")

    with pytest.raises(DuplicateTenantError):
        await admin.create_tenant("acme")


async def test_created_key_resolves_to_its_tenant(
    store: InMemoryTenantStore, admin: TenantAdmin
) -> None:
    tenant = await admin.create_tenant("acme")

    issued = await admin.create_key("acme")

    assert await store.tenant_for_key(hash_key(issued.plaintext)) == tenant


async def test_key_for_unknown_tenant_is_an_error(admin: TenantAdmin) -> None:
    with pytest.raises(TenantNotFoundError):
        await admin.create_key("nobody")


async def test_revoked_key_no_longer_resolves(
    store: InMemoryTenantStore, admin: TenantAdmin
) -> None:
    await admin.create_tenant("acme")
    issued = await admin.create_key("acme")

    await admin.revoke_key(issued.prefix)

    assert await store.tenant_for_key(issued.key_hash) is None


async def test_revoking_drops_the_key_from_the_cache(
    store: InMemoryTenantStore, cache: InMemoryKeyCache, admin: TenantAdmin
) -> None:
    tenant = await admin.create_tenant("acme")
    issued = await admin.create_key("acme")
    await cache.remember(issued.key_hash, tenant)

    await admin.revoke_key(issued.prefix)

    assert await cache.tenant_for_key(issued.key_hash) is None


async def test_revoking_an_unknown_prefix_is_an_error(admin: TenantAdmin) -> None:
    with pytest.raises(KeyNotFoundError):
        await admin.revoke_key("deadbeef")


async def test_revoking_with_the_cache_down_still_revokes_and_says_so(
    store: InMemoryTenantStore,
) -> None:
    admin = TenantAdmin(store, UnreachableKeyCache())
    await admin.create_tenant("acme")
    issued = await admin.create_key("acme")

    with pytest.raises(KeyCacheNotClearedError, match=issued.prefix):
        await admin.revoke_key(issued.prefix)

    assert await store.tenant_for_key(issued.key_hash) is None
