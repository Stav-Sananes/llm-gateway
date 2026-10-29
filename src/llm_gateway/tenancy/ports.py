"""What tenant and key administration needs from storage."""

from typing import Protocol

from llm_gateway.tenancy.model import StoredKey, Tenant


class DuplicateTenantError(Exception):
    """A tenant with this name already exists."""


class KeyNotFoundError(Exception):
    """No key has this prefix."""


class TenantStore(Protocol):
    async def add_tenant(self, tenant: Tenant) -> None:
        """Add a new tenant. Raises DuplicateTenantError if the name is taken."""
        ...

    async def tenant_named(self, name: str) -> Tenant | None:
        """Get a tenant by name."""
        ...

    async def add_key(self, key: StoredKey) -> None:
        """Add a new key for an existing tenant."""
        ...

    async def revoke_key(self, prefix: str) -> None:
        """Revoke a key. Raises KeyNotFoundError if no key has this prefix."""
        ...

    async def tenant_for_key(self, key_hash: str) -> Tenant | None:
        """Get the tenant owning this key, or None if the key is unknown or revoked."""
        ...
