"""Redis-backed KeyCache. Entries expire, so a missed eviction can't keep a revoked key alive."""

import json
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from llm_gateway.tenancy.model import Tenant
from llm_gateway.tenancy.ports import KeyCacheUnavailableError

_KEY_PREFIX = "gw:key:"


class RedisKeyCache:
    def __init__(self, redis: Redis, ttl_s: int) -> None:
        self._redis = redis
        self._ttl_s = ttl_s

    async def tenant_for_key(self, key_hash: str) -> Tenant | None:
        try:
            cached = await self._redis.get(_KEY_PREFIX + key_hash)
        except RedisError as error:
            raise KeyCacheUnavailableError(str(error)) from error
        return None if cached is None else _tenant(cached)

    async def remember(self, key_hash: str, tenant: Tenant) -> None:
        entry = json.dumps({"id": str(tenant.id), "name": tenant.name})
        try:
            await self._redis.set(_KEY_PREFIX + key_hash, entry, ex=self._ttl_s)
        except RedisError as error:
            raise KeyCacheUnavailableError(str(error)) from error

    async def forget(self, key_hash: str) -> None:
        try:
            await self._redis.delete(_KEY_PREFIX + key_hash)
        except RedisError as error:
            raise KeyCacheUnavailableError(str(error)) from error


def _tenant(cached: bytes | str) -> Tenant:
    fields = json.loads(cached)
    return Tenant(id=UUID(fields["id"]), name=fields["name"])
