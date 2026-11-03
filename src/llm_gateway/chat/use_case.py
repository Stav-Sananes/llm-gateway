"""The chat completion use case: enforce the tenant's limits, then stream from the provider.

Limits are checked before the first chunk exists, so a refusal is still a plain HTTP
error. Tokens are reserved for the worst case up front and settled against the
provider's reported usage when the stream ends, however it ends.
"""

import logging
from dataclasses import replace

from llm_gateway.chat.model import ChatRequest, Usage
from llm_gateway.chat.ports import ChatProvider, ChunkStream
from llm_gateway.limits.estimate import estimate_prompt_tokens
from llm_gateway.limits.ports import (
    BudgetUnavailableError,
    RateLimiter,
    Reservation,
    TokenBudget,
)
from llm_gateway.tenancy.model import Tenant

logger = logging.getLogger(__name__)


class ChatCompletions:
    def __init__(
        self,
        provider: ChatProvider,
        rate_limiter: RateLimiter,
        budget: TokenBudget,
        default_max_tokens: int,
    ) -> None:
        self._provider = provider
        self._rate_limiter = rate_limiter
        self._budget = budget
        self._default_max_tokens = default_max_tokens

    async def start(self, request: ChatRequest, tenant: Tenant) -> ChunkStream:
        """Raises RateLimitedError, BudgetExhaustedError or BudgetUnavailableError up front."""
        await self._rate_limiter.acquire(tenant.id)
        # Cap the completion upstream too, so the reservation really is the worst case.
        max_tokens = request.max_tokens or self._default_max_tokens
        worst_case = estimate_prompt_tokens(request.messages) + max_tokens
        reservation = await self._budget.reserve(tenant.id, worst_case)
        return self._metered(
            self._provider.stream(replace(request, max_tokens=max_tokens)), reservation
        )

    async def _metered(self, chunks: ChunkStream, reservation: Reservation) -> ChunkStream:
        started = False
        usage: Usage | None = None
        try:
            async for chunk in chunks:
                started = True
                usage = chunk.usage or usage
                yield chunk
        finally:
            await chunks.aclose()
            await self._settle(reservation, _tokens_used(reservation, started, usage))

    async def _settle(self, reservation: Reservation, used: int) -> None:
        try:
            await self._budget.settle(reservation, used)
        except BudgetUnavailableError:
            logger.warning("token budget unavailable at settle; reservation stays charged")


def _tokens_used(reservation: Reservation, started: bool, usage: Usage | None) -> int:
    if usage is not None:
        return usage.total_tokens
    # Nothing came back (provider refused or was down): nothing to charge.
    # Output came back but no usage report (the stream broke): charge the upper bound.
    return reservation.tokens if started else 0
