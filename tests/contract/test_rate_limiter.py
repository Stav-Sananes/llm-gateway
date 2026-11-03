"""Behaviour every RateLimiter must have: each test runs in memory and against real Redis."""

import asyncio
from collections.abc import AsyncIterator, Callable
from uuid import uuid4

import pytest

from llm_gateway.config import Settings
from llm_gateway.limits.ports import RateLimitedError, RateLimiter
from llm_gateway.limits.token_bucket import InMemoryRateLimiter
from llm_gateway.main import build_redis
from llm_gateway.storage.rate_limiter import RedisRateLimiter

pytestmark = pytest.mark.anyio

LimiterFactory = Callable[[int, float], RateLimiter]


@pytest.fixture(params=["memory", "redis"])
async def limiter_with(request: pytest.FixtureRequest) -> AsyncIterator[LimiterFactory]:
    if request.param == "memory":
        yield InMemoryRateLimiter
        return
    redis = build_redis(Settings(redis_url=request.getfixturevalue("redis_url")))
    try:
        await redis.flushdb()
        yield lambda capacity, per_s: RedisRateLimiter(redis, capacity, per_s)
    finally:
        await redis.aclose()


async def test_allows_a_burst_then_refuses_with_a_retry_time(limiter_with: LimiterFactory) -> None:
    limiter, tenant = limiter_with(3, 1.0), uuid4()
    for _ in range(3):
        await limiter.acquire(tenant)

    with pytest.raises(RateLimitedError) as refused:
        await limiter.acquire(tenant)

    assert 0 < refused.value.retry_after_s <= 1.0


async def test_refills_over_time(limiter_with: LimiterFactory) -> None:
    limiter, tenant = limiter_with(1, 20.0), uuid4()
    await limiter.acquire(tenant)

    await asyncio.sleep(0.1)  # two tokens' worth at 20 per second; the bucket holds one

    await limiter.acquire(tenant)


async def test_an_idle_bucket_never_holds_more_than_its_burst(
    limiter_with: LimiterFactory,
) -> None:
    limiter, tenant = limiter_with(2, 50.0), uuid4()
    await limiter.acquire(tenant)

    await asyncio.sleep(0.2)  # ten tokens' worth of refill

    await limiter.acquire(tenant)
    await limiter.acquire(tenant)
    with pytest.raises(RateLimitedError):
        await limiter.acquire(tenant)


async def test_tenants_do_not_share_a_bucket(limiter_with: LimiterFactory) -> None:
    limiter = limiter_with(1, 0.1)
    await limiter.acquire(uuid4())

    await limiter.acquire(uuid4())


async def test_concurrent_requests_never_exceed_the_burst(limiter_with: LimiterFactory) -> None:
    limiter, tenant = limiter_with(5, 0.01), uuid4()

    results = await asyncio.gather(
        *(limiter.acquire(tenant) for _ in range(20)), return_exceptions=True
    )

    assert sum(result is None for result in results) == 5
