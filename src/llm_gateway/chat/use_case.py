"""The chat completion use case: enforce the tenant's limits, then stream from the provider.

Limits are checked before the first chunk exists, so a refusal is still a plain HTTP
error. Tokens are reserved for the worst case up front and settled against the
provider's reported usage when the stream ends, however it ends; then one usage event
records what happened.
"""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import anyio

from llm_gateway.chat.model import ChatRequest, CompletionChunk, Origin, Usage
from llm_gateway.chat.ports import ChatProvider, ChunkStream
from llm_gateway.limits.estimate import estimate_prompt_tokens
from llm_gateway.limits.ports import (
    BudgetUnavailableError,
    RateLimiter,
    Reservation,
    TokenBudget,
)
from llm_gateway.tenancy.model import Tenant
from llm_gateway.usage.model import Outcome, UsageEvent
from llm_gateway.usage.ports import NoUsageRecording, UsageRecorder

logger = logging.getLogger(__name__)

# Settling and recording after the stream ends may take this long, even when the client
# has gone and the request is being cancelled.
FINISH_TIMEOUT_S = 5.0
NO_USAGE_RECORDING = NoUsageRecording()


class ChatCompletions:
    def __init__(
        self,
        provider: ChatProvider,
        rate_limiter: RateLimiter,
        budget: TokenBudget,
        default_max_tokens: int,
        usage: UsageRecorder = NO_USAGE_RECORDING,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._provider = provider
        self._rate_limiter = rate_limiter
        self._budget = budget
        self._default_max_tokens = default_max_tokens
        self._usage = usage
        self._clock = clock

    async def start(self, request: ChatRequest, tenant: Tenant) -> ChunkStream:
        """Raises RateLimitedError, BudgetExhaustedError or BudgetUnavailableError up front."""
        await self._rate_limiter.acquire(tenant.id)
        # Cap the completion upstream too, so the reservation really is the worst case.
        max_tokens = request.max_tokens or self._default_max_tokens
        worst_case = estimate_prompt_tokens(request.messages) + max_tokens
        reservation = await self._budget.reserve(tenant.id, worst_case)
        meter = _Meter(request.model, datetime.now(UTC), self._clock(), self._clock)
        return self._metered(
            self._provider.stream(replace(request, max_tokens=max_tokens)), reservation, meter
        )

    async def _metered(
        self, chunks: ChunkStream, reservation: Reservation, meter: "_Meter"
    ) -> ChunkStream:
        try:
            async for chunk in chunks:
                meter.saw(chunk)
                yield chunk
        finally:
            # Shielded: when the client disconnects, the request is cancelled and this runs
            # inside the cancellation; unshielded, the settle would never happen and the
            # tenant would stay charged the worst case.
            with anyio.move_on_after(FINISH_TIMEOUT_S, shield=True) as finishing:
                await chunks.aclose()
                charged = _tokens_used(reservation, meter.started, meter.usage)
                await self._settle(reservation, charged)
                await self._usage.record(meter.event(reservation, charged))
            if finishing.cancelled_caught:
                logger.error("request finish took over %ss; settle or usage lost", FINISH_TIMEOUT_S)

    async def _settle(self, reservation: Reservation, used: int) -> None:
        try:
            await self._budget.settle(reservation, used)
        except BudgetUnavailableError:
            logger.warning("token budget unavailable at settle; reservation stays charged")


@dataclass
class _Meter:
    """What one request's stream did, as it happens."""

    requested_model: str
    started_at: datetime
    started_clock: float
    clock: Callable[[], float]
    origin: Origin | None = None
    usage: Usage | None = None
    first_chunk_clock: float | None = None

    @property
    def started(self) -> bool:
        return self.first_chunk_clock is not None

    def saw(self, chunk: CompletionChunk) -> None:
        if self.first_chunk_clock is None:
            self.first_chunk_clock = self.clock()
        self.origin = chunk.origin or self.origin
        self.usage = chunk.usage or self.usage

    def event(self, reservation: Reservation, charged: int) -> UsageEvent:
        usage = self.usage or Usage(0, 0)
        return UsageEvent(
            event_id=uuid.uuid4(),
            tenant_id=reservation.tenant_id,
            requested_model=self.requested_model,
            provider=self.origin.provider if self.origin else None,
            model=self.origin.model if self.origin else None,
            outcome=self._outcome(),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            charged_tokens=charged,
            started_at=self.started_at,
            duration_s=self.clock() - self.started_clock,
            time_to_first_chunk_s=(
                None
                if self.first_chunk_clock is None
                else self.first_chunk_clock - self.started_clock
            ),
        )

    def _outcome(self) -> Outcome:
        if self.usage is not None:
            return Outcome.COMPLETED
        return Outcome.INTERRUPTED if self.started else Outcome.FAILED


def _tokens_used(reservation: Reservation, started: bool, usage: Usage | None) -> int:
    if usage is not None:
        return usage.total_tokens
    # Nothing came back (provider refused or was down): nothing to charge.
    # Output came back but no usage report (the stream broke): charge the upper bound.
    return reservation.tokens if started else 0
