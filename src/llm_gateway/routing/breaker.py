"""A circuit breaker per provider: stop calling a provider that keeps failing, try it again later.

closed ──N failures in a row──► open ──reset_after_s──► half-open ──trial ok──► closed
                                  ▲                         │
                                  └──────trial fails────────┘
"""

import logging
import time
from collections.abc import Callable
from enum import StrEnum

from llm_gateway.chat.model import ChatRequest
from llm_gateway.chat.ports import ChatProvider, ChunkStream, ProviderUnavailableError

logger = logging.getLogger(__name__)

Clock = Callable[[], float]


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


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

    def try_acquire(self) -> bool:
        """May a call go through now? In half-open, only one trial call at a time."""
        state = self.state
        if state is BreakerState.CLOSED:
            return True
        if state is BreakerState.HALF_OPEN and not self._trial_running:
            self._trial_running = True
            return True
        return False

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

    def release(self) -> None:
        """A call ended without telling us anything (the client left early): free the trial."""
        self._trial_running = False


class GuardedProvider:
    """A provider behind a circuit breaker. The provider counts as healthy once it streams."""

    def __init__(self, provider: ChatProvider, breaker: CircuitBreaker) -> None:
        self._provider = provider
        self.breaker = breaker

    async def stream(self, request: ChatRequest) -> ChunkStream:
        if not self.breaker.try_acquire():
            raise ProviderUnavailableError(f"{self.breaker.name} circuit is open")
        chunks = self._provider.stream(request)
        answered = False
        try:
            async for chunk in chunks:
                if not answered:
                    answered = True
                    self.breaker.record_success()
                yield chunk
        except ProviderUnavailableError:
            self.breaker.record_failure()
            raise
        finally:
            await chunks.aclose()
            if not answered:
                self.breaker.release()
