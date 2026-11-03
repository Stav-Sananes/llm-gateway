"""Redis token bucket per tenant, shared by every gateway replica.

One Lua script reads, refills and takes a token atomically, using Redis's own clock,
so replicas never race each other or disagree about time.
"""

from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from llm_gateway.limits.ports import LimiterUnavailableError, RateLimitedError

_TAKE_FROM_BUCKET = """
local capacity = tonumber(ARGV[1])
local refill_per_s = tonumber(ARGV[2])
local time = redis.call('TIME')
local now = tonumber(time[1]) + tonumber(time[2]) / 1000000
local state = redis.call('HMGET', KEYS[1], 'tokens', 'updated_at')
local tokens = tonumber(state[1]) or capacity
local updated_at = tonumber(state[2]) or now
tokens = math.min(capacity, tokens + math.max(0, now - updated_at) * refill_per_s)
local retry_after = 0
if tokens >= 1 then
  tokens = tokens - 1
else
  retry_after = (1 - tokens) / refill_per_s
end
redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated_at', now)
redis.call('EXPIRE', KEYS[1], math.ceil(capacity / refill_per_s) + 1)
return tostring(retry_after)
"""


class RedisRateLimiter:
    def __init__(self, redis: Redis, capacity: int, refill_per_s: float) -> None:
        self._take_from_bucket = redis.register_script(_TAKE_FROM_BUCKET)
        self._capacity = capacity
        self._refill_per_s = refill_per_s

    async def acquire(self, tenant_id: UUID) -> None:
        try:
            retry_after = await self._take_from_bucket(
                keys=[f"gw:rate:{tenant_id}"], args=[self._capacity, self._refill_per_s]
            )
        except RedisError as error:
            raise LimiterUnavailableError(str(error)) from error
        if float(retry_after) > 0:
            raise RateLimitedError(float(retry_after))
