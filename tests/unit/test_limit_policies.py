from uuid import UUID

import pytest

from llm_gateway.limits.policies import FailOpenRateLimiter
from llm_gateway.limits.ports import RateLimitedError
from llm_gateway.limits.token_bucket import InMemoryRateLimiter
from tests.support.limits import UnreachableRateLimiter

pytestmark = pytest.mark.anyio

ACME = UUID(int=1)


async def test_shared_limiter_down_falls_back_to_the_local_limit() -> None:
    limiter = FailOpenRateLimiter(
        shared=UnreachableRateLimiter(), local=InMemoryRateLimiter(capacity=1, refill_per_s=0.1)
    )

    await limiter.acquire(ACME)  # open...
    with pytest.raises(RateLimitedError):  # ...but still limited
        await limiter.acquire(ACME)


async def test_a_refusal_from_the_shared_limiter_is_not_retried_locally() -> None:
    shared = InMemoryRateLimiter(capacity=0, refill_per_s=0.1)
    limiter = FailOpenRateLimiter(shared=shared, local=InMemoryRateLimiter(10, 10.0))

    with pytest.raises(RateLimitedError):
        await limiter.acquire(ACME)
