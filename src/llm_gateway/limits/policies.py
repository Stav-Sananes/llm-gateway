"""What happens when the shared limit stores fail: rate limits fail open, budgets fail closed."""

import logging
from uuid import UUID

from llm_gateway.limits.ports import LimiterUnavailableError, RateLimiter, Reservation

logger = logging.getLogger(__name__)


class FailOpenRateLimiter:
    """Uses the shared limiter, and a local one while the shared limiter is unreachable.

    Failing open is safe here because the token budget, which fails closed, still caps spend.
    """

    def __init__(self, shared: RateLimiter, local: RateLimiter) -> None:
        self._shared = shared
        self._local = local

    async def acquire(self, tenant_id: UUID) -> None:
        try:
            await self._shared.acquire(tenant_id)
        except LimiterUnavailableError:
            logger.warning("shared rate limiter unavailable; limiting per replica")
            await self._local.acquire(tenant_id)


class UnlimitedBudget:
    """The budget used when no token budget is configured."""

    async def reserve(self, tenant_id: UUID, tokens: int) -> Reservation:
        return Reservation(tenant_id=tenant_id, period="unlimited", tokens=tokens)

    async def settle(self, reservation: Reservation, used_tokens: int) -> None:
        return None
