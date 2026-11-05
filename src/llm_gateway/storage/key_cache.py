"""Redis-backed KeyCache.

Each entry records when its key was last confirmed against Postgres. It is fresh for
`fresh_s`; after that it is re-checked, but Redis keeps it for `stale_s`, so that while
Postgres is down a key that was confirmed recently still works. Entries expire, so a missed
revoke mark can't keep a key alive for longer than that.
"""

import json
import time
from collections.abc import Callable
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from llm_gateway.tenancy.model import Tenant
from llm_gateway.tenancy.ports import (
    REVOKED,
    CachedTenant,
    KeyCacheUnavailableError,
    Revoked,
)

_KEY_PREFIX = "gw:key:"
_REVOKED_MARK = b"revoked"
# Replace the entry only if it is still a tenant: a revoke mark written meanwhile wins.
_REFRESH = """
local current = redis.call('GET', KEYS[1])
if current and current ~= ARGV[3] then
  return redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
end
return nil
"""


class RedisKeyCache:
    def __init__(
        self,
        redis: Redis,
        fresh_s: float,
        stale_s: float | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis
        self._fresh_s = fresh_s
        self._stale_s = int(max(stale_s or fresh_s, fresh_s))
        self._mark_s = int(fresh_s)
        self._clock = clock
        self._refresh = redis.register_script(_REFRESH)

    async def tenant_for_key(self, key_hash: str) -> CachedTenant | Revoked | None:
        try:
            cached: bytes | str | None = await self._redis.get(_KEY_PREFIX + key_hash)
            if cached is None:
                return None
            if cached == _REVOKED_MARK:
                return REVOKED
            entry = self._entry(cached)
            if entry is None:
                # Unreadable entry: drop it and treat it as a miss, so the store answers.
                await self._redis.delete(_KEY_PREFIX + key_hash)
            return entry
        except RedisError as error:
            raise KeyCacheUnavailableError(str(error)) from error

    async def remember(self, key_hash: str, tenant: Tenant) -> None:
        try:
            await self._redis.set(
                _KEY_PREFIX + key_hash, self._encoded(tenant), ex=self._stale_s, nx=True
            )
        except RedisError as error:
            raise KeyCacheUnavailableError(str(error)) from error

    async def refresh(self, key_hash: str, tenant: Tenant) -> None:
        try:
            await self._refresh(
                keys=[_KEY_PREFIX + key_hash],
                args=[self._encoded(tenant), self._stale_s, _REVOKED_MARK],
            )
        except RedisError as error:
            raise KeyCacheUnavailableError(str(error)) from error

    async def mark_revoked(self, key_hash: str) -> None:
        try:
            await self._redis.set(_KEY_PREFIX + key_hash, _REVOKED_MARK, ex=self._mark_s)
        except RedisError as error:
            raise KeyCacheUnavailableError(str(error)) from error

    def _encoded(self, tenant: Tenant) -> str:
        return json.dumps({"id": str(tenant.id), "name": tenant.name, "checked_at": self._clock()})

    def _entry(self, cached: bytes | str) -> CachedTenant | None:
        try:
            fields = json.loads(cached)
            tenant = Tenant(id=UUID(fields["id"]), name=fields["name"])
            # An entry from before checked_at existed counts as stale: it gets re-checked.
            checked_at = float(fields.get("checked_at", 0))
        except (ValueError, KeyError, TypeError):
            return None
        return CachedTenant(tenant, fresh=self._clock() - checked_at < self._fresh_s)
