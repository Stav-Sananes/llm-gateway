"""The chat completion use case: enforce the tenant's limits, then stream from the provider.

Limits are checked before the first chunk exists, so a refusal is still a plain HTTP
error. Tokens are reserved for the worst case up front and settled against the
provider's reported usage when the stream ends, however it ends.
"""

import logging

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
        reservation = await self._budget.reserve(tenant.id, self._worst_case_tokens(request))
        return self._metered(self._provider.stream(request), reservation)

    def _worst_case_tokens(self, request: ChatRequest) -> int:
        completion = request.max_tokens or self._default_max_tokens
        return estimate_prompt_tokens(request.messages) + completion

    async def _metered(self, chunks: ChunkStream, reservation: Reservation) -> ChunkStream:
        usage: Usage | None = None
        try:
            async for chunk in chunks:
                usage = chunk.usage or usage
                yield chunk
        finally:
            await chunks.aclose()
            await self._settle(reservation, usage)

    async def _settle(self, reservation: Reservation, usage: Usage | None) -> None:
        # No usage reported (the stream broke): keep charging the whole reservation.
        used = reservation.tokens if usage is None else usage.total_tokens
        try:
            await self._budget.settle(reservation, used)
        except BudgetUnavailableError:
            logger.warning("token budget unavailable at settle; reservation stays charged")
