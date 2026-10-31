"""What only the Redis adapter does: expire entries, survive bad entries, bound its waits."""

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from llm_gateway.config import Settings
from llm_gateway.main import build_redis
from llm_gateway.storage.key_cache import RedisKeyCache
from llm_gateway.tenancy.keys import issue_key
from llm_gateway.tenancy.model import Tenant
from llm_gateway.tenancy.ports import KeyCacheUnavailableError
from tests.support.servers import silent_local_url, unused_local_url

pytestmark = pytest.mark.anyio

ACME = Tenant(id=uuid4(), name="acme")
TIMEOUT_S = 0.25

CacheCall = Callable[[RedisKeyCache, str], Awaitable[Any]]
CALLS: dict[str, CacheCall] = {
    "lookup": lambda cache, key_hash: cache.tenant_for_key(key_hash),
    "remember": lambda cache, key_hash: cache.remember(key_hash, ACME),
    "mark_revoked": lambda cache, key_hash: cache.mark_revoked(key_hash),
}


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = build_redis(Settings(redis_url=redis_url))
    try:
        await client.flushdb()
        yield client
    finally:
        await client.aclose()


@pytest.mark.parametrize("call", [CALLS["remember"], CALLS["mark_revoked"]], ids=["tenant", "mark"])
async def test_every_entry_expires(redis: Redis, call: CacheCall) -> None:
    key_hash = issue_key().key_hash

    await call(RedisKeyCache(redis, ttl_s=30), key_hash)

    assert 0 < await redis.ttl(f"gw:key:{key_hash}") <= 30


async def test_unreadable_entry_is_a_miss_and_is_removed(redis: Redis) -> None:
    key_hash = issue_key().key_hash
    await redis.set(f"gw:key:{key_hash}", b"not json")

    assert await RedisKeyCache(redis, ttl_s=30).tenant_for_key(key_hash) is None
    assert await redis.exists(f"gw:key:{key_hash}") == 0


@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
async def test_refused_connection_is_reported(call: CacheCall) -> None:
    redis = build_redis(Settings(redis_url=unused_local_url().replace("http://", "redis://")))
    try:
        with pytest.raises(KeyCacheUnavailableError):
            await call(RedisKeyCache(redis, ttl_s=60), issue_key().key_hash)
    finally:
        await redis.aclose()


@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
async def test_hung_redis_is_given_up_on_after_the_timeout(call: CacheCall) -> None:
    with silent_local_url() as url:
        settings = Settings(redis_url=url.replace("http://", "redis://"), redis_timeout_s=TIMEOUT_S)
        redis = build_redis(settings)
        started = time.monotonic()
        try:
            with pytest.raises(KeyCacheUnavailableError):
                await call(RedisKeyCache(redis, ttl_s=60), issue_key().key_hash)
        finally:
            await redis.aclose()

    # Bounded by our timeout, not redis-py's defaults (which would wait forever).
    assert time.monotonic() - started < 4 * TIMEOUT_S
