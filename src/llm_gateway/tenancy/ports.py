"""What tenant and key administration needs from storage."""

from typing import Protocol

from llm_gateway.tenancy.model import StoredKey, Tenant


class DuplicateTenantError(Exception):
    """A tenant with this name already exists."""


class KeyNotFoundError(Exception):
    """No key has this prefix."""


class TenantNotFoundError(Exception):
    """Tenant not found."""


class TenantStoreUnavailableError(Exception):
    """The tenant store could not be reached in time."""


class KeyCacheUnavailableError(Exception):
    """The key cache could not be reached in time."""


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

    async def key_with_prefix(self, prefix: str) -> StoredKey | None:
        """Get a key by its prefix, revoked or not."""
        ...

    async def revoke_key(self, prefix: str) -> None:
        """Revoke a key. Raises KeyNotFoundError if no key has this prefix."""
        ...

    async def tenant_for_key(self, key_hash: str) -> Tenant | None:
        """Get the tenant owning this key, or None if the key is unknown or revoked.

        Raises TenantStoreUnavailableError when the store can't be reached.
        """
        ...


class Revoked:
    """What the cache holds for a revoked key, so a late cache write can't bring it back."""


REVOKED = Revoked()


class KeyCache(Protocol):
    """A fast copy of key-hash-to-tenant lookups. Postgres stays the source of truth.

    Every method raises KeyCacheUnavailableError when the cache can't be reached.
    """

    async def tenant_for_key(self, key_hash: str) -> Tenant | Revoked | None:
        """Get the cached tenant for this key hash, REVOKED if it is marked revoked, or None."""
        ...

    async def remember(self, key_hash: str, tenant: Tenant) -> None:
        """Cache the tenant for a limited time, unless the key hash already has an entry.

        Never overwriting is what stops a lookup that raced a revoke from re-caching the key.
        """
        ...

    async def mark_revoked(self, key_hash: str) -> None:
        """Replace any entry for this key hash with REVOKED, for a limited time."""
        ...
