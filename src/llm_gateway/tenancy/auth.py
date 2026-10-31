"""Turns the API key a client presents into the tenant it belongs to."""

import logging
from contextlib import suppress

from llm_gateway.tenancy.keys import hash_key, looks_like_key
from llm_gateway.tenancy.model import Tenant
from llm_gateway.tenancy.ports import KeyCache, KeyCacheUnavailableError, Revoked, TenantStore

logger = logging.getLogger(__name__)

_UNKNOWN_OR_REVOKED = "unknown or revoked API key"


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
        try:
            cached = await self._cache.tenant_for_key(key_hash)
        except KeyCacheUnavailableError:
            logger.warning("key cache unavailable; authenticating against the store")
            # Don't try to cache the result: it would wait out a second cache timeout.
            return await self._stored_tenant(key_hash)
        if isinstance(cached, Revoked):
            raise InvalidApiKeyError(_UNKNOWN_OR_REVOKED)
        if cached is not None:
            return cached
        tenant = await self._stored_tenant(key_hash)
        with suppress(KeyCacheUnavailableError):
            await self._cache.remember(key_hash, tenant)
        return tenant

    async def _stored_tenant(self, key_hash: str) -> Tenant:
        tenant = await self._store.tenant_for_key(key_hash)
        if tenant is None:
            raise InvalidApiKeyError(_UNKNOWN_OR_REVOKED)
        return tenant
