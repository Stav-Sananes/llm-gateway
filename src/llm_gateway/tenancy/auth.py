"""Turns the API key a client presents into the tenant it belongs to."""

import logging
from contextlib import suppress

from llm_gateway.tenancy.keys import hash_key, looks_like_key
from llm_gateway.tenancy.model import Tenant
from llm_gateway.tenancy.ports import (
    KeyCache,
    KeyCacheUnavailableError,
    Revoked,
    TenantStore,
    TenantStoreUnavailableError,
)

logger = logging.getLogger(__name__)

_UNKNOWN_OR_REVOKED = "unknown or revoked API key"


class InvalidApiKeyError(Exception):
    """The presented key is malformed, unknown or revoked."""


class Authenticator:
    """Looks a key up in the cache first and in the store when the cache has no fresh entry.

    When the cache is down, every lookup goes to the store: slower, but still correct. When
    the store is down, a key the cache confirmed recently (a stale entry) still works, so a
    Postgres outage doesn't stop every tenant once the fresh entries run out.
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
        if cached is not None and cached.fresh:
            return cached.tenant
        try:
            tenant = await self._stored_tenant(key_hash)
        except TenantStoreUnavailableError:
            if cached is None:
                raise  # never confirmed: "not now", never "invalid"
            logger.warning("tenant store unavailable; accepting a key confirmed earlier")
            return cached.tenant
        except InvalidApiKeyError:
            if cached is not None:  # revoked since it was cached, and the mark was missed
                with suppress(KeyCacheUnavailableError):
                    await self._cache.mark_revoked(key_hash)
            raise
        with suppress(KeyCacheUnavailableError):
            if cached is None:
                await self._cache.remember(key_hash, tenant)
            else:
                await self._cache.refresh(key_hash, tenant)
        return tenant

    async def _stored_tenant(self, key_hash: str) -> Tenant:
        tenant = await self._store.tenant_for_key(key_hash)
        if tenant is None:
            raise InvalidApiKeyError(_UNKNOWN_OR_REVOKED)
        return tenant
