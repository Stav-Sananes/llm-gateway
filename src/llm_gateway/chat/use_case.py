"""The chat completion use case: enforce the tenant's limits, then stream from the provider.

Limits are checked before the first chunk exists, so a refusal is still a plain HTTP
error. Tokens are reserved for the worst case up front and settled against the
provider's reported usage when the stream ends, however it ends; then one usage event
records what happened.

Between the budget and the provider sits the semantic cache: a hit is replayed for free,
and a miss's answer is remembered once it has finished cleanly.
"""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

import anyio

from llm_gateway.cache.model import (
    DEFAULT_DIRECTIVES,
    MAX_CACHED_ANSWER_CHARS,
    CachedAnswer,
    CacheDirectives,
)
from llm_gateway.cache.semantic import Hit, Lookup, Miss, SemanticCache, replay
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
        finish_timeout_s: float = FINISH_TIMEOUT_S,
        cache: SemanticCache | None = None,
    ) -> None:
        self._provider = provider
        self._rate_limiter = rate_limiter
        self._budget = budget
        self._default_max_tokens = default_max_tokens
        self._usage = usage
        self._clock = clock
        self._finish_timeout_s = finish_timeout_s
        self._cache = cache

    async def start(
        self,
        request: ChatRequest,
        tenant: Tenant,
        directives: CacheDirectives = DEFAULT_DIRECTIVES,
    ) -> ChunkStream:
        """Raises RateLimitedError, BudgetExhaustedError or BudgetUnavailableError up front."""
        await self._rate_limiter.acquire(tenant.id)
        # Cap the completion upstream too, so the reservation really is the worst case.
        max_tokens = request.max_tokens or self._default_max_tokens
        worst_case = estimate_prompt_tokens(request.messages) + max_tokens
        reservation = await self._budget.reserve(tenant.id, worst_case)
        meter = _Meter(request.model, datetime.now(UTC), self._clock(), self._clock)
        # The cache keys on the request as the client sent it, max_tokens included.
        lookup = await self._look_up(tenant, request, directives)
        chunks = (
            replay(lookup)
            if isinstance(lookup, Hit)
            else self._provider.stream(replace(request, max_tokens=max_tokens))
        )
        return self._metered(chunks, reservation, meter, lookup)

    async def _look_up(
        self, tenant: Tenant, request: ChatRequest, directives: CacheDirectives
    ) -> Lookup:
        if self._cache is None:
            return None
        return await self._cache.look_up(tenant.id, request, directives)

    async def _metered(
        self, chunks: ChunkStream, reservation: Reservation, meter: "_Meter", lookup: Lookup
    ) -> ChunkStream:
        answer = _AnswerText(keep=isinstance(lookup, Miss))
        try:
            async for chunk in chunks:
                meter.saw(chunk)
                answer.saw(chunk)
                yield chunk
        finally:
            # Shielded: when the client disconnects, the request is cancelled and this runs
            # inside the cancellation; unshielded, the settle would never happen and the
            # tenant would stay charged the worst case.
            with anyio.move_on_after(self._finish_timeout_s, shield=True) as finishing:
                await chunks.aclose()
                charged = _tokens_used(reservation, meter.started, meter.usage)
                await self._settle(reservation, charged)
                await self._usage.record(meter.event(reservation, charged))
                if isinstance(lookup, Miss) and meter.usage is not None and meter.origin:
                    await self._remember(lookup, answer.finished(meter.origin.model))
            if finishing.cancelled_caught:
                logger.error(
                    "request finish took over %ss; settle or usage lost", self._finish_timeout_s
                )

    async def _remember(self, miss: Miss, answer: CachedAnswer | None) -> None:
        if self._cache is not None and answer is not None:
            await self._cache.remember(miss, answer)

    async def _settle(self, reservation: Reservation, used: int) -> None:
        try:
            await self._budget.settle(reservation, used)
        except BudgetUnavailableError:
            logger.warning("token budget unavailable at settle; reservation stays charged")


@dataclass
class _AnswerText:
    """The streamed answer, collected only when it may go into the cache."""

    keep: bool
    parts: list[str] = field(default_factory=list)
    length: int = 0
    finish_reason: str | None = None

    def saw(self, chunk: CompletionChunk) -> None:
        if not self.keep:
            return
        self.length += len(chunk.content)
        if self.length > MAX_CACHED_ANSWER_CHARS:  # too long to keep: stop collecting
            self.keep, self.parts = False, []
            return
        if chunk.content:
            self.parts.append(chunk.content)
        self.finish_reason = chunk.finish_reason or self.finish_reason

    def finished(self, model: str) -> CachedAnswer | None:
        if not self.keep or self.finish_reason is None:
            return None
        return CachedAnswer("".join(self.parts), self.finish_reason, model)


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
