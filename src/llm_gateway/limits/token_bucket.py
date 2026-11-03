"""Token-bucket rate limiting kept in process memory.

Used on its own in tests, and as the fallback when the shared Redis limiter is down:
each gateway replica then limits on its own, which is looser but never unlimited.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from llm_gateway.limits.ports import RateLimitedError

Clock = Callable[[], float]


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class InMemoryRateLimiter:
    def __init__(self, capacity: int, refill_per_s: float, clock: Clock = time.monotonic) -> None:
        self._capacity = capacity
        self._refill_per_s = refill_per_s
        self._clock = clock
        self._buckets: dict[UUID, _Bucket] = {}

    async def acquire(self, tenant_id: UUID) -> None:
        now = self._clock()
        bucket = self._buckets.setdefault(tenant_id, _Bucket(self._capacity, now))
        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(self._capacity, bucket.tokens + elapsed * self._refill_per_s)
        bucket.updated_at = now
        if bucket.tokens < 1:
            raise RateLimitedError((1 - bucket.tokens) / self._refill_per_s)
        bucket.tokens -= 1
