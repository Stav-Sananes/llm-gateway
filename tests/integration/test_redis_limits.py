"""What only the Redis limit adapters do: report outages, settle across midnight."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from llm_gateway.config import Settings
from llm_gateway.limits.ports import BudgetUnavailableError, LimiterUnavailableError, Reservation
from llm_gateway.main import build_redis
from llm_gateway.storage.rate_limiter import RedisRateLimiter
from llm_gateway.storage.token_budget import RedisTokenBudget
from tests.support.servers import unused_local_url

pytestmark = pytest.mark.anyio


@pytest.fixture
async def down_redis() -> AsyncIterator[Redis]:
    redis = build_redis(Settings(redis_url=unused_local_url().replace("http://", "redis://")))
    yield redis
    await redis.aclose()


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = build_redis(Settings(redis_url=redis_url))
    await client.flushdb()
    yield client
    await client.aclose()


async def test_limiter_reports_redis_down(down_redis: Redis) -> None:
    with pytest.raises(LimiterUnavailableError):
        await RedisRateLimiter(down_redis, 10, 1.0).acquire(uuid4())


async def test_budget_reports_redis_down_on_reserve_and_settle(down_redis: Redis) -> None:
    budget = RedisTokenBudget(down_redis, 100)
    with pytest.raises(BudgetUnavailableError):
        await budget.reserve(uuid4(), 10)
    with pytest.raises(BudgetUnavailableError):
        await budget.settle(Reservation(uuid4(), "2026-11-03", 10), 5)


async def test_settle_after_midnight_lands_in_the_reserving_day(redis: Redis) -> None:
    clock = [datetime(2026, 11, 3, 23, 59, 59, tzinfo=UTC)]
    budget = RedisTokenBudget(redis, 100, now=lambda: clock[0])
    tenant = uuid4()
    reservation = await budget.reserve(tenant, 80)

    clock[0] += timedelta(seconds=2)
    await budget.settle(reservation, 30)

    assert await redis.get(f"gw:budget:{tenant}:2026-11-03") == b"30"
    await budget.reserve(tenant, 100)  # the new day starts empty


async def test_budget_counters_expire(redis: Redis) -> None:
    tenant = uuid4()
    reservation = await RedisTokenBudget(redis, 100).reserve(tenant, 10)

    assert await redis.ttl(f"gw:budget:{tenant}:{reservation.period}") > 0


async def test_settle_after_redis_lost_the_counter_creates_nothing(redis: Redis) -> None:
    budget, tenant = RedisTokenBudget(redis, 100), uuid4()
    reservation = await budget.reserve(tenant, 80)
    await redis.flushdb()  # a restart without persistence, or a failover

    await budget.settle(reservation, 12)

    assert await redis.exists(f"gw:budget:{tenant}:{reservation.period}") == 0


async def test_settle_refreshes_the_counter_expiry(redis: Redis) -> None:
    budget, tenant = RedisTokenBudget(redis, 100), uuid4()
    reservation = await budget.reserve(tenant, 80)
    key = f"gw:budget:{tenant}:{reservation.period}"
    await redis.expire(key, 5)

    await budget.settle(reservation, 12)

    assert await redis.ttl(key) > 5


async def test_rate_limit_buckets_expire_once_they_would_be_full_again(redis: Redis) -> None:
    tenant = uuid4()

    await RedisRateLimiter(redis, capacity=10, refill_per_s=2.0).acquire(tenant)

    assert 0 < await redis.ttl(f"gw:rate:{tenant}") <= 6  # 10 / 2 s + 1
