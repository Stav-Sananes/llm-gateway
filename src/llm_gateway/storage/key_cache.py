"""Redis-backed KeyCache. Entries expire, so a missed revoke mark can't keep a key alive."""

import json
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from llm_gateway.tenancy.model import Tenant
from llm_gateway.tenancy.ports import REVOKED, KeyCacheUnavailableError, Revoked

_KEY_PREFIX = "gw:key:"
_REVOKED_MARK = b"revoked"


class RedisKeyCache:
    def __init__(self, redis: Redis, ttl_s: int) -> None:
        self._redis = redis
        self._ttl_s = ttl_s

    async def tenant_for_key(self, key_hash: str) -> Tenant | Revoked | None:
        try:
            cached: bytes | str | None = await self._redis.get(_KEY_PREFIX + key_hash)
            if cached is None:
                return None
            if cached == _REVOKED_MARK:
                return REVOKED
            tenant = _tenant(cached)
            if tenant is None:
                # Unreadable entry: drop it and treat it as a miss, so the store answers.
                await self._redis.delete(_KEY_PREFIX + key_hash)
            return tenant
        except RedisError as error:
            raise KeyCacheUnavailableError(str(error)) from error

    async def remember(self, key_hash: str, tenant: Tenant) -> None:
        entry = json.dumps({"id": str(tenant.id), "name": tenant.name})
        try:
            await self._redis.set(_KEY_PREFIX + key_hash, entry, ex=self._ttl_s, nx=True)
        except RedisError as error:
            raise KeyCacheUnavailableError(str(error)) from error

    async def mark_revoked(self, key_hash: str) -> None:
        try:
            await self._redis.set(_KEY_PREFIX + key_hash, _REVOKED_MARK, ex=self._ttl_s)
        except RedisError as error:
            raise KeyCacheUnavailableError(str(error)) from error


def _tenant(cached: bytes | str) -> Tenant | None:
    try:
        fields = json.loads(cached)
        return Tenant(id=UUID(fields["id"]), name=fields["name"])
    except (ValueError, KeyError, TypeError):
        return None
