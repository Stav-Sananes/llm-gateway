from uuid import UUID

import pytest

from llm_gateway.limits.ports import RateLimitedError
from llm_gateway.limits.token_bucket import InMemoryRateLimiter

pytestmark = pytest.mark.anyio

ACME, GLOBEX = UUID(int=1), UUID(int=2)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


async def take(limiter: InMemoryRateLimiter, tenant: UUID, times: int) -> None:
    for _ in range(times):
        await limiter.acquire(tenant)


async def test_a_full_bucket_allows_a_burst_then_refuses() -> None:
    limiter = InMemoryRateLimiter(capacity=3, refill_per_s=1.0, clock=FakeClock())
    await take(limiter, ACME, 3)

    with pytest.raises(RateLimitedError) as refused:
        await limiter.acquire(ACME)

    assert refused.value.retry_after_s == pytest.approx(1.0)


async def test_the_bucket_refills_over_time() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(capacity=2, refill_per_s=4.0, clock=clock)
    await take(limiter, ACME, 2)

    clock.now += 0.25  # one token at 4 per second

    await limiter.acquire(ACME)
    with pytest.raises(RateLimitedError):
        await limiter.acquire(ACME)


async def test_the_bucket_never_holds_more_than_its_capacity() -> None:
    clock = FakeClock()
    limiter = InMemoryRateLimiter(capacity=2, refill_per_s=1.0, clock=clock)

    clock.now += 3600

    await take(limiter, ACME, 2)
    with pytest.raises(RateLimitedError):
        await limiter.acquire(ACME)


async def test_each_tenant_has_its_own_bucket() -> None:
    limiter = InMemoryRateLimiter(capacity=1, refill_per_s=1.0, clock=FakeClock())
    await limiter.acquire(ACME)

    await limiter.acquire(GLOBEX)
