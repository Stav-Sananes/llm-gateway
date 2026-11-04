"""The chat completion use case: enforce the tenant's limits, then stream from the provider.

Limits are checked before the first chunk exists, so a refusal is still a plain HTTP
error. Tokens are reserved for the worst case up front and settled against the
provider's reported usage when the stream ends, however it ends; then one usage event
records what happened.

Before the budget sits the semantic cache: a hit is replayed for free, and a miss's answer
is remembered once it has finished cleanly. When the router resumes an answer on another
provider mid-stream, each provider gets its own usage event and the switch is recorded.
"""

import itertools
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID

import anyio

from llm_gateway.cache.model import (
    DEFAULT_DIRECTIVES,
    MAX_CACHED_ANSWER_CHARS,
    CachedAnswer,
    CacheDirectives,
)
from llm_gateway.cache.semantic import Hit, Lookup, Miss, SemanticCache, replay
from llm_gateway.chat.model import ChatRequest, CompletionChunk, Origin, Usage
from llm_gateway.chat.ports import ChatProvider, ChunkStream, NoSwitchRecording, ProviderSwitches
from llm_gateway.limits.estimate import estimate_prompt_tokens, estimate_text_tokens
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
NO_SWITCH_RECORDING = NoSwitchRecording()


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
        switches: ProviderSwitches = NO_SWITCH_RECORDING,
    ) -> None:
        self._provider = provider
        self._rate_limiter = rate_limiter
        self._budget = budget
        self._default_max_tokens = default_max_tokens
        self._usage = usage
        self._clock = clock
        self._finish_timeout_s = finish_timeout_s
        self._cache = cache
        self._switches = switches

    async def start(
        self,
        request: ChatRequest,
        tenant: Tenant,
        directives: CacheDirectives = DEFAULT_DIRECTIVES,
    ) -> ChunkStream:
        """Raises RateLimitedError, BudgetExhaustedError or BudgetUnavailableError up front."""
        await self._rate_limiter.acquire(tenant.id)
        meter = _Meter(
            tenant.id,
            request.model,
            estimate_prompt_tokens(request.messages),
            datetime.now(UTC),
            self._clock(),
            self._clock,
        )
        # Before the reservation: a hit costs nothing, and a lookup that fails or is
        # cancelled can't leave a reservation behind. It keys on the request as the client
        # sent it, max_tokens included.
        lookup = await self._look_up(tenant, request, directives)
        if isinstance(lookup, Hit):
            return self._metered(replay(lookup), None, meter, lookup)
        # Cap the completion upstream too, so the reservation really is the worst case.
        max_tokens = request.max_tokens or self._default_max_tokens
        worst_case = meter.prompt_estimate + max_tokens
        reservation = await self._budget.reserve(tenant.id, worst_case)
        chunks = self._provider.stream(replace(request, max_tokens=max_tokens))
        return self._metered(chunks, reservation, meter, lookup)

    async def _look_up(
        self, tenant: Tenant, request: ChatRequest, directives: CacheDirectives
    ) -> Lookup:
        if self._cache is None:
            return None
        return await self._cache.look_up(tenant.id, request, directives)

    async def _metered(
        self,
        chunks: ChunkStream,
        reservation: Reservation | None,
        meter: "_Meter",
        lookup: Lookup,
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
                events = meter.events(reservation)
                if reservation is not None:  # a cache hit reserved nothing
                    await self._settle(reservation, sum(e.charged_tokens for e in events))
                for event in events:
                    await self._usage.record(event)
                for dead, resumed in meter.switches():
                    await self._switches.record(
                        meter.tenant_id, meter.requested_model, dead, resumed
                    )
                if isinstance(lookup, Miss) and (model := meter.answered_by()):
                    self._remember(lookup, answer.finished(model))
            if finishing.cancelled_caught:
                logger.error(
                    "request finish took over %ss; settle or usage lost", self._finish_timeout_s
                )

    def _remember(self, miss: Miss, answer: CachedAnswer | None) -> None:
        if self._cache is not None and answer is not None:
            self._cache.remember(miss, answer)

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
class _Segment:
    """What one provider streamed of the answer."""

    origin: Origin | None
    first_chunk_clock: float
    completion_estimate: int = 0  # from the text, for a provider that died before reporting
    usage: Usage | None = None


@dataclass
class _Meter:
    """What one request's stream did, as it happens. A new Origin mid-stream means another
    provider took over the answer: a new segment, and its own usage event."""

    tenant_id: UUID
    requested_model: str
    prompt_estimate: int
    started_at: datetime
    started_clock: float
    clock: Callable[[], float]
    segments: list[_Segment] = field(default_factory=list)

    def saw(self, chunk: CompletionChunk) -> None:
        if not self.segments or chunk.origin is not None:
            self.segments.append(_Segment(chunk.origin, self.clock()))
        segment = self.segments[-1]
        segment.completion_estimate += estimate_text_tokens(chunk.content)
        segment.usage = chunk.usage or segment.usage

    def answered_by(self) -> str | None:
        """The model, when one provider answered the whole request and reported usage."""
        if len(self.segments) != 1:
            return None
        (segment,) = self.segments
        if segment.usage is None or segment.origin is None:
            return None
        return segment.origin.model

    def switches(self) -> list[tuple[Origin, Origin]]:
        origins = [s.origin for s in self.segments if s.origin is not None]
        return list(itertools.pairwise(origins))

    def events(self, reservation: Reservation | None) -> list[UsageEvent]:
        """One event per provider that served the request (one with no provider if none did).

        Charges: a reported usage as reported; a provider that died and was continued, the
        estimate of its prompt and its output; an answer that ended without any usage
        report, the rest of the reservation (the worst case, as nothing better is known)."""
        if not self.segments:
            return [self._event(None, Outcome.FAILED, Usage(0, 0), 0)]
        events: list[UsageEvent] = []
        *continued, last = self.segments
        for segment in continued:
            usage = Usage(self.prompt_estimate, segment.completion_estimate)
            events.append(self._event(segment, Outcome.RESUMED, usage, usage.total_tokens))
        if last.usage is not None:
            events.append(self._event(last, Outcome.COMPLETED, last.usage, last.usage.total_tokens))
        else:
            so_far = sum(e.charged_tokens for e in events)
            rest = max(0, reservation.tokens - so_far) if reservation is not None else 0
            events.append(self._event(last, Outcome.INTERRUPTED, Usage(0, 0), rest))
        return events

    def _event(
        self, segment: _Segment | None, outcome: Outcome, usage: Usage, charged: int
    ) -> UsageEvent:
        origin = segment.origin if segment else None
        return UsageEvent(
            event_id=uuid.uuid4(),
            tenant_id=self.tenant_id,
            requested_model=self.requested_model,
            provider=origin.provider if origin else None,
            model=origin.model if origin else None,
            outcome=outcome,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            charged_tokens=charged,
            started_at=self.started_at,
            duration_s=self.clock() - self.started_clock,
            time_to_first_chunk_s=(
                segment.first_chunk_clock - self.started_clock if segment else None
            ),
        )
