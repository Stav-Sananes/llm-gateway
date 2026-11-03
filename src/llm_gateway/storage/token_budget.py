"""Redis token budget per tenant per UTC day: reserve atomically, settle when done."""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from llm_gateway.limits.ports import BudgetExhaustedError, BudgetUnavailableError, Reservation

# Check and add in one step, so two concurrent requests can't both squeeze under the limit.
_RESERVE = """
local used = tonumber(redis.call('GET', KEYS[1]) or '0')
if used + tonumber(ARGV[2]) > tonumber(ARGV[1]) then
  return -1
end
redis.call('INCRBY', KEYS[1], ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""

# Only adjust a counter that still exists: if Redis lost it, a refund must not create a
# negative counter with no expiry (free budget, forever).
_SETTLE = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
redis.call('INCRBY', KEYS[1], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""

_KEEP_S = 2 * 24 * 3600  # a period's counter outlives the period, for late settles

Now = Callable[[], datetime]


class RedisTokenBudget:
    def __init__(
        self, redis: Redis, tokens_per_day: int, now: Now = lambda: datetime.now(UTC)
    ) -> None:
        self._redis = redis
        self._reserve = redis.register_script(_RESERVE)
        self._settle = redis.register_script(_SETTLE)
        self._tokens_per_day = tokens_per_day
        self._now = now

    async def reserve(self, tenant_id: UUID, tokens: int) -> Reservation:
        period = self._now().date().isoformat()
        try:
            reserved = await self._reserve(
                keys=[_key(tenant_id, period)], args=[self._tokens_per_day, tokens, _KEEP_S]
            )
        except RedisError as error:
            raise BudgetUnavailableError(str(error)) from error
        if reserved != 1:
            raise BudgetExhaustedError(f"tenant {tenant_id} is out of tokens for {period}")
        return Reservation(tenant_id=tenant_id, period=period, tokens=tokens)

    async def settle(self, reservation: Reservation, used_tokens: int) -> None:
        # Settle against the period the tokens were reserved in, even after midnight.
        try:
            await self._settle(
                keys=[_key(reservation.tenant_id, reservation.period)],
                args=[used_tokens - reservation.tokens, _KEEP_S],
            )
        except RedisError as error:
            raise BudgetUnavailableError(str(error)) from error


def _key(tenant_id: UUID, period: str) -> str:
    return f"gw:budget:{tenant_id}:{period}"
