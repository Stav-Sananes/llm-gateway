import pytest

from llm_gateway.tenancy.admin import TenantAdmin
from llm_gateway.tenancy.keys import hash_key
from llm_gateway.tenancy.ports import DuplicateTenantError, TenantNotFoundError
from tests.support.in_memory_tenant_store import InMemoryTenantStore

pytestmark = pytest.mark.anyio


@pytest.fixture
def store() -> InMemoryTenantStore:
    return InMemoryTenantStore()


@pytest.fixture
def admin(store: InMemoryTenantStore) -> TenantAdmin:
    return TenantAdmin(store)


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
