"""Admission: refuse new work while this process is too busy to do it well.

What runs out first in one gateway process is its event loop: every streamed chunk costs a
little CPU, and once the loop falls behind, everything slows down together (Redis calls
hit their timeouts, connections time out, circuits open on healthy providers). So new
requests are refused (503, Retry-After) while the loop runs late, and past a hard cap on
requests in flight. Idle streams waiting on a slow model cost almost nothing, so the cap
is high: it is a backstop, the lag is the signal.

Admission happens after the body is read and the key checked: a client that never
finishes sending its body, or has no valid key, holds nothing here.
"""

import random
from collections.abc import Callable
from dataclasses import dataclass

import anyio

LAG_SAMPLE_S = 0.05
_SMOOTHING = 0.3  # weight of the newest sample: one blip (a GC pause) doesn't shed alone


class AtCapacityError(Exception):
    """This process is too busy to start the request now; retry shortly."""

    def __init__(self, retry_after_s: int) -> None:
        super().__init__("at capacity")
        self.retry_after_s = retry_after_s


class LoopLag:
    """How late the event loop runs: a task sleeps LAG_SAMPLE_S at a time, and the time
    it actually took beyond that is the lag, smoothed over recent samples. Run it in the
    app's lifespan."""

    def __init__(self) -> None:
        self.lag_s = 0.0

    async def run(self) -> None:
        while True:
            started = anyio.current_time()
            await anyio.sleep(LAG_SAMPLE_S)
            sample = max(0.0, anyio.current_time() - started - LAG_SAMPLE_S)
            self.lag_s += _SMOOTHING * (sample - self.lag_s)


@dataclass(frozen=True)
class AdmissionLimits:
    max_in_flight: int = 1024
    max_loop_lag_s: float = 0.05


class Admission:
    """One per kind of work (chat, tool calls), so one kind can't use up the other's room."""

    def __init__(
        self,
        lag: LoopLag,
        limits: AdmissionLimits = AdmissionLimits(),  # noqa: B008 - frozen
        on_shed: Callable[[], None] = lambda: None,
        jitter: Callable[[], int] = lambda: random.randint(1, 3),  # noqa: S311 - not secret
    ) -> None:
        self._lag = lag
        self._limits = limits
        self._on_shed = on_shed
        self._jitter = jitter
        self._in_flight = 0  # one event loop: no lock needed

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def admit(self) -> "Ticket":
        """A ticket to release when the work is done. Raises AtCapacityError."""
        busy = self._lag.lag_s > self._limits.max_loop_lag_s
        if busy or self._in_flight >= self._limits.max_in_flight:
            self._on_shed()
            # Jittered, so refused clients don't all come back in the same second.
            raise AtCapacityError(self._jitter())
        self._in_flight += 1
        return Ticket(self)

    def _release(self) -> None:
        self._in_flight -= 1


class Ticket:
    def __init__(self, admission: Admission) -> None:
        self._admission: Admission | None = admission

    def release(self) -> None:
        """Idempotent: the first call frees the place."""
        if self._admission is not None:
            self._admission._release()
            self._admission = None
