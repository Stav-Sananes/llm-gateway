"""Behaviour every KeyCache must have: each test runs against the fake and real Redis."""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from llm_gateway.config import Settings
from llm_gateway.main import build_redis
from llm_gateway.storage.key_cache import RedisKeyCache
from llm_gateway.tenancy.keys import issue_key
from llm_gateway.tenancy.model import Tenant
from llm_gateway.tenancy.ports import REVOKED, KeyCache
from tests.support.in_memory_key_cache import InMemoryKeyCache

pytestmark = pytest.mark.anyio

ACME = Tenant(id=uuid4(), name="acme")


@pytest.fixture(params=["memory", "redis"])
async def cache(request: pytest.FixtureRequest) -> AsyncIterator[KeyCache]:
    if request.param == "memory":
        yield InMemoryKeyCache()
        return
    redis = build_redis(Settings(redis_url=request.getfixturevalue("redis_url")))
    try:
        await redis.flushdb()
        yield RedisKeyCache(redis, ttl_s=60)
    finally:
        await redis.aclose()


async def test_remembered_tenant_is_found(cache: KeyCache) -> None:
    key_hash = issue_key().key_hash

    await cache.remember(key_hash, ACME)

    assert await cache.tenant_for_key(key_hash) == ACME


async def test_unknown_key_hash_is_a_miss(cache: KeyCache) -> None:
    assert await cache.tenant_for_key(issue_key().key_hash) is None


async def test_revoked_mark_replaces_a_cached_tenant(cache: KeyCache) -> None:
    key_hash = issue_key().key_hash
    await cache.remember(key_hash, ACME)

    await cache.mark_revoked(key_hash)

    assert await cache.tenant_for_key(key_hash) is REVOKED


async def test_remember_never_overwrites_a_revoked_mark(cache: KeyCache) -> None:
    # A lookup that read Postgres just before a revoke must not bring the key back.
    key_hash = issue_key().key_hash
    await cache.mark_revoked(key_hash)

    await cache.remember(key_hash, ACME)

    assert await cache.tenant_for_key(key_hash) is REVOKED


async def test_remember_keeps_the_first_entry(cache: KeyCache) -> None:
    key_hash = issue_key().key_hash
    await cache.remember(key_hash, ACME)

    await cache.remember(key_hash, Tenant(id=uuid4(), name="globex"))

    assert await cache.tenant_for_key(key_hash) == ACME
