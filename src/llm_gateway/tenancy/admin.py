import uuid

from llm_gateway.tenancy.keys import IssuedKey, issue_key
from llm_gateway.tenancy.model import StoredKey, Tenant
from llm_gateway.tenancy.ports import TenantNotFoundError, TenantStore


class TenantAdmin:
    def __init__(self, store: TenantStore) -> None:
        self._store = store

    async def create_tenant(self, name: str) -> Tenant:
        """
        Create a new tenant with the given name.
        """
        tenant_id = uuid.uuid4()
        tenant = Tenant(id=tenant_id, name=name)
        await self._store.add_tenant(tenant)
        return tenant

    async def create_key(self, tenant_name: str) -> IssuedKey:
        """
        Issue a key for the tenant. The plaintext is returned once; only its hash is stored.
        """
        tenant = await self._store.tenant_named(name=tenant_name)
        if tenant is None:
            raise TenantNotFoundError(f"no tenant named {tenant_name!r}")
        issued_key = issue_key()
        await self._store.add_key(
            StoredKey(
                tenant_id=tenant.id,
                prefix=issued_key.prefix,
                key_hash=issued_key.key_hash,
            )
        )
        return issued_key

    async def revoke_key(self, prefix: str) -> None:
        """
        Revoke a key. Raises KeyNotFoundError if no key has this prefix.
        """
        await self._store.revoke_key(prefix=prefix)
