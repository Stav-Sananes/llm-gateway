"""Turns the API key a client presents into the tenant it belongs to."""

import logging
from contextlib import suppress

from llm_gateway.tenancy.keys import hash_key, looks_like_key
from llm_gateway.tenancy.model import Tenant
from llm_gateway.tenancy.ports import KeyCache, KeyCacheUnavailableError, TenantStore

logger = logging.getLogger(__name__)


class InvalidApiKeyError(Exception):
    """The presented key is malformed, unknown or revoked."""


class Authenticator:
    """Looks a key up in the cache first and in the store on a miss.

    When the cache is down, every lookup goes to the store: slower, but still correct.
    """

    def __init__(self, store: TenantStore, cache: KeyCache) -> None:
        self._store = store
        self._cache = cache

    async def authenticate(self, presented_key: str) -> Tenant:
        if not looks_like_key(presented_key):
            raise InvalidApiKeyError("malformed API key")
        key_hash = hash_key(presented_key)
        tenant = await self._cached_tenant(key_hash)
        if tenant is not None:
            return tenant
        tenant = await self._store.tenant_for_key(key_hash)
        if tenant is None:
            raise InvalidApiKeyError("unknown or revoked API key")
        await self._remember(key_hash, tenant)
        return tenant

    async def _cached_tenant(self, key_hash: str) -> Tenant | None:
        try:
            return await self._cache.tenant_for_key(key_hash)
        except KeyCacheUnavailableError:
            logger.warning("key cache unavailable; authenticating against the store")
            return None

    async def _remember(self, key_hash: str, tenant: Tenant) -> None:
        with suppress(KeyCacheUnavailableError):
            await self._cache.remember(key_hash, tenant)
