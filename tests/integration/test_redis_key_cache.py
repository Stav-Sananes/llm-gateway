"""What only the Redis adapter does: expire entries and report an unreachable Redis."""

import time
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import pytest

from llm_gateway.config import Settings
from llm_gateway.main import build_redis
from llm_gateway.storage.key_cache import RedisKeyCache
from llm_gateway.tenancy.keys import issue_key
from llm_gateway.tenancy.model import Tenant
from llm_gateway.tenancy.ports import KeyCacheUnavailableError
from tests.support.servers import unused_local_url

pytestmark = pytest.mark.anyio

ACME = Tenant(id=uuid4(), name="acme")

CacheCall = Callable[[RedisKeyCache, str], Awaitable[Any]]
CALLS: dict[str, CacheCall] = {
    "lookup": lambda cache, key_hash: cache.tenant_for_key(key_hash),
    "remember": lambda cache, key_hash: cache.remember(key_hash, ACME),
    "forget": lambda cache, key_hash: cache.forget(key_hash),
}


async def test_entries_expire_after_the_ttl(redis_url: str) -> None:
    redis = build_redis(Settings(redis_url=redis_url))
    key_hash = issue_key().key_hash
    try:
        await RedisKeyCache(redis, ttl_s=30).remember(key_hash, ACME)

        assert 0 < await redis.ttl(f"gw:key:{key_hash}") <= 30
    finally:
        await redis.aclose()


@pytest.mark.parametrize("call", CALLS.values(), ids=CALLS.keys())
async def test_unreachable_redis_is_reported_quickly(call: CacheCall) -> None:
    url = unused_local_url().replace("http://", "redis://")
    redis = build_redis(Settings(redis_url=url, redis_timeout_s=0.25))
    started = time.monotonic()
    try:
        with pytest.raises(KeyCacheUnavailableError):
            await call(RedisKeyCache(redis, ttl_s=60), issue_key().key_hash)
    finally:
        await redis.aclose()

    assert time.monotonic() - started < 2.0
