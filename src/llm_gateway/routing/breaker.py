"""A circuit breaker per provider: stop calling a provider that keeps failing, try it again later.

closed ──N failures in a row──► open ──reset_after_s──► half-open ──trial ok──► closed
                                  ▲                         │
                                  └──────trial fails────────┘
"""

import logging
import time
from collections.abc import Callable
from enum import Enum, StrEnum

from llm_gateway.chat.model import ChatRequest
from llm_gateway.chat.ports import (
    ChatProvider,
    ChunkStream,
    GatewayBusyError,
    ProviderUnavailableError,
)

logger = logging.getLogger(__name__)

Clock = Callable[[], float]


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class Permit(Enum):
    NORMAL = "normal"
    TRIAL = "trial"


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int,
        reset_after_s: float,
        clock: Clock = time.monotonic,
    ) -> None:
        self.name = name
        self._failure_threshold = failure_threshold
        self._reset_after_s = reset_after_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_running = False

    @property
    def state(self) -> BreakerState:
        if self._opened_at is None:
            return BreakerState.CLOSED
        if self._clock() - self._opened_at >= self._reset_after_s:
            return BreakerState.HALF_OPEN
        return BreakerState.OPEN

    def try_acquire(self) -> Permit | None:
        """May a call go through now? In half-open, only one trial call at a time."""
        state = self.state
        if state is BreakerState.CLOSED:
            return Permit.NORMAL
        if state is BreakerState.HALF_OPEN and not self._trial_running:
            self._trial_running = True
            return Permit.TRIAL
        return None

    def record_success(self) -> None:
        if self._opened_at is not None:
            logger.info("circuit %s closed", self.name)
        self._failures, self._opened_at, self._trial_running = 0, None, False

    def record_failure(self) -> None:
        self._failures += 1
        if self._trial_running or self._failures >= self._failure_threshold:
            if self._opened_at is None:
                logger.warning("circuit %s opened", self.name)
            # (Re)open: a failed trial starts the wait over.
            self._opened_at, self._trial_running = self._clock(), False

    def release(self, permit: Permit) -> None:
        """A call ended without telling us anything (the client left early).

        Only the trial's own call frees the trial; an older call ending says nothing about it.
        """
        if permit is Permit.TRIAL:
            self._trial_running = False


class GuardedProvider:
    """A provider behind a circuit breaker. A call counts as a success only once its stream
    has ended cleanly: a provider that keeps dying mid-answer must open its circuit, even
    though resume (chat/resume.py) hides each death from the client."""

    def __init__(self, provider: ChatProvider, breaker: CircuitBreaker) -> None:
        self._provider = provider
        self.breaker = breaker

    async def stream(self, request: ChatRequest) -> ChunkStream:
        permit = self.breaker.try_acquire()
        if permit is None:
            raise ProviderUnavailableError(f"{self.breaker.name} circuit is open")
        chunks = self._provider.stream(request)
        outcome_recorded = False
        try:
            async for chunk in chunks:
                yield chunk
            self.breaker.record_success()
            outcome_recorded = True
        except GatewayBusyError:
            raise  # our own saturation says nothing about the provider: permit released below
        except ProviderUnavailableError:
            self.breaker.record_failure()
            outcome_recorded = True
            raise
        finally:
            await chunks.aclose()
            if not outcome_recorded:  # rejected, or the client left: says nothing either way
                self.breaker.release(permit)
