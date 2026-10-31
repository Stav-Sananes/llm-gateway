"""Behaviour every TenantStore must have: each test runs against the fake and real Postgres."""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from llm_gateway.config import Settings
from llm_gateway.main import build_engine
from llm_gateway.storage.tenant_store import PostgresTenantStore
from llm_gateway.tenancy.keys import issue_key
from llm_gateway.tenancy.model import StoredKey, Tenant
from llm_gateway.tenancy.ports import DuplicateTenantError, KeyNotFoundError, TenantStore
from tests.support.in_memory_tenant_store import InMemoryTenantStore
from tests.support.postgres import empty_tables

pytestmark = pytest.mark.anyio


@pytest.fixture(params=["memory", "postgres"])
async def store(request: pytest.FixtureRequest) -> AsyncIterator[TenantStore]:
    if request.param == "memory":
        yield InMemoryTenantStore()
        return
    engine = build_engine(Settings(database_url=request.getfixturevalue("postgres_url")))
    try:
        await empty_tables(engine)
        yield PostgresTenantStore(engine)
    finally:
        await engine.dispose()


def a_tenant(name: str = "acme") -> Tenant:
    return Tenant(id=uuid4(), name=name)


async def add_key_for(store: TenantStore, tenant: Tenant) -> StoredKey:
    issued = issue_key()
    key = StoredKey(tenant_id=tenant.id, prefix=issued.prefix, key_hash=issued.key_hash)
    await store.add_key(key)
    return key


async def test_added_tenant_is_found_by_name(store: TenantStore) -> None:
    tenant = a_tenant("acme")
    await store.add_tenant(tenant)

    assert await store.tenant_named("acme") == tenant


async def test_unknown_tenant_name_is_not_found(store: TenantStore) -> None:
    assert await store.tenant_named("nobody") is None


async def test_tenant_names_are_unique(store: TenantStore) -> None:
    await store.add_tenant(a_tenant("acme"))

    with pytest.raises(DuplicateTenantError):
        await store.add_tenant(a_tenant("acme"))


async def test_key_hash_finds_its_tenant(store: TenantStore) -> None:
    tenant = a_tenant()
    await store.add_tenant(tenant)
    key = await add_key_for(store, tenant)

    assert await store.tenant_for_key(key.key_hash) == tenant


async def test_unknown_key_hash_finds_no_tenant(store: TenantStore) -> None:
    assert await store.tenant_for_key(issue_key().key_hash) is None


async def test_revoked_key_no_longer_finds_its_tenant(store: TenantStore) -> None:
    tenant = a_tenant()
    await store.add_tenant(tenant)
    key = await add_key_for(store, tenant)

    await store.revoke_key(key.prefix)

    assert await store.tenant_for_key(key.key_hash) is None


async def test_revoking_one_key_leaves_the_tenants_other_keys_working(
    store: TenantStore,
) -> None:
    tenant = a_tenant()
    await store.add_tenant(tenant)
    revoked = await add_key_for(store, tenant)
    kept = await add_key_for(store, tenant)

    await store.revoke_key(revoked.prefix)

    assert await store.tenant_for_key(kept.key_hash) == tenant


async def test_revoking_an_unknown_prefix_is_an_error(store: TenantStore) -> None:
    with pytest.raises(KeyNotFoundError):
        await store.revoke_key("deadbeef")
