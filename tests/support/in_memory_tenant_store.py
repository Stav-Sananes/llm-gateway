"""Dict-backed TenantStore for tests. Revoked keys are kept and marked, as Postgres will do."""

from uuid import UUID

from llm_gateway.tenancy.model import StoredKey, Tenant
from llm_gateway.tenancy.ports import DuplicateTenantError, KeyNotFoundError


class InMemoryTenantStore:
    def __init__(self) -> None:
        self._tenants_by_name: dict[str, Tenant] = {}
        self._keys_by_prefix: dict[str, StoredKey] = {}
        self._revoked_prefixes: set[str] = set()

    async def add_tenant(self, tenant: Tenant) -> None:
        if tenant.name in self._tenants_by_name:
            raise DuplicateTenantError(f"tenant {tenant.name!r} already exists")
        self._tenants_by_name[tenant.name] = tenant

    async def tenant_named(self, name: str) -> Tenant | None:
        return self._tenants_by_name.get(name)

    async def add_key(self, key: StoredKey) -> None:
        self._keys_by_prefix[key.prefix] = key

    async def key_with_prefix(self, prefix: str) -> StoredKey | None:
        return self._keys_by_prefix.get(prefix)

    async def revoke_key(self, prefix: str) -> None:
        if prefix not in self._keys_by_prefix:
            raise KeyNotFoundError(f"no key with prefix {prefix!r}")
        self._revoked_prefixes.add(prefix)

    async def tenant_for_key(self, key_hash: str) -> Tenant | None:
        key = self._active_key_with_hash(key_hash)
        if key is None:
            return None
        return self._tenant_with_id(key.tenant_id)

    def _active_key_with_hash(self, key_hash: str) -> StoredKey | None:
        for key in self._keys_by_prefix.values():
            if key.key_hash == key_hash and key.prefix not in self._revoked_prefixes:
                return key
        return None

    def _tenant_with_id(self, tenant_id: UUID) -> Tenant | None:
        for tenant in self._tenants_by_name.values():
            if tenant.id == tenant_id:
                return tenant
        return None
