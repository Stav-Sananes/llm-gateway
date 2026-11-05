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
    """A provider behind a circuit breaker and a bulkhead.

    A call counts as a success for the breaker only once its stream has ended cleanly: a
    provider that keeps dying mid-answer must open its circuit, even though resume
    (chat/resume.py) hides each death from the client.

    The bulkhead caps the calls in flight to this provider (its connection pool's size).
    Past it, a call fails at once with GatewayBusyError, which doesn't count against the
    breaker: the router moves on to the next target instead of waiting for a connection,
    and one slow provider can't take every place in the gateway."""

    def __init__(
        self, provider: ChatProvider, breaker: CircuitBreaker, max_in_flight: int = 1000
    ) -> None:
        self._provider = provider
        self.breaker = breaker
        self._max_in_flight = max_in_flight
        self._in_flight = 0  # one event loop: no lock needed

    async def stream(self, request: ChatRequest) -> ChunkStream:
        if self._in_flight >= self._max_in_flight:
            raise GatewayBusyError(f"{self.breaker.name}: {self._max_in_flight} calls in flight")
        permit = self.breaker.try_acquire()
        if permit is None:
            raise ProviderUnavailableError(f"{self.breaker.name} circuit is open")
        self._in_flight += 1
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
            try:
                await chunks.aclose()  # hands the connection back to the pool
            finally:
                # Only now: freed earlier, the next call could take the slot and find no
                # free connection.
                self._in_flight -= 1
                if not outcome_recorded:  # rejected, or the client left: says nothing
                    self.breaker.release(permit)
