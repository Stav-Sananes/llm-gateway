"""Helpers for tests that run against the real Postgres and Redis containers."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from llm_gateway.config import Settings
from llm_gateway.main import build_engine, build_redis
from llm_gateway.storage.key_cache import RedisKeyCache
from llm_gateway.storage.tenant_store import PostgresTenantStore
from llm_gateway.tenancy.admin import TenantAdmin
from tests.support.postgres import empty_tables


def reset(settings: Settings) -> None:
    """Empty the tables and the cache. Called at the start of a test, never the end."""
    asyncio.run(_reset(settings))


def new_api_key(settings: Settings, tenant_name: str = "acme") -> str:
    """Create a tenant and one key for it; return the key's plaintext."""
    return asyncio.run(_new_api_key(settings, tenant_name))


def revoke(settings: Settings, plaintext_key: str) -> None:
    asyncio.run(_revoke(settings, plaintext_key.split("_", 2)[1]))


@asynccontextmanager
async def real_store_and_cache(
    settings: Settings,
) -> AsyncIterator[tuple[PostgresTenantStore, RedisKeyCache]]:
    engine = build_engine(settings)
    redis = build_redis(settings)
    try:
        yield PostgresTenantStore(engine), RedisKeyCache(redis, settings.key_cache_ttl_s)
    finally:
        await redis.aclose()
        await engine.dispose()


async def _reset(settings: Settings) -> None:
    engine = build_engine(settings)
    redis = build_redis(settings)
    try:
        await empty_tables(engine)
        await redis.flushdb()
    finally:
        await redis.aclose()
        await engine.dispose()


async def _new_api_key(settings: Settings, tenant_name: str) -> str:
    async with real_store_and_cache(settings) as (store, cache):
        admin = TenantAdmin(store, cache)
        await admin.create_tenant(tenant_name)
        return (await admin.create_key(tenant_name)).plaintext


async def _revoke(settings: Settings, prefix: str) -> None:
    async with real_store_and_cache(settings) as (store, cache):
        await TenantAdmin(store, cache).revoke_key(prefix)
