"""Admission: refuse new work while this process is too busy to do it well.

What runs out first in one gateway process is its event loop: every streamed chunk costs a
little CPU, and once the loop falls behind, everything slows down together (Redis calls
hit their timeouts, connections time out, circuits open on healthy providers). So new
requests are refused (503, Retry-After) while the loop runs late, and past a hard cap on
requests in flight. Idle streams waiting on a slow model cost almost nothing, so the cap
is high: it is a backstop, the lag is the signal.

Two checks, in two places:
- The lag is checked at the door (ShedWhileLagging), before the body is read or the key
  checked: refusing must cost almost nothing, or the work done before refusing keeps the
  loop busy. That check holds nothing, so it can't be abused to hold places.
- A place in flight is taken after the body is read and the key checked (admit()): a
  client that never finishes sending its body, or has no valid key, holds nothing here.
"""

import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import NoReturn

import anyio
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from llm_gateway.http.presenter import error_payload

LAG_SAMPLE_S = 0.05
# Weight of the newest sample. At the default 50 ms threshold, a single late sample sheds
# only when it is over about 170 ms (0.3 x 170 = 51): ordinary GC pauses don't shed alone.
_SMOOTHING = 0.3


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

    def refuse_if_lagging(self) -> None:
        """The check at the door: raises AtCapacityError while the event loop runs late."""
        if self._lagging():
            self._refuse()

    def admit(self) -> "Ticket":
        """A ticket to release when the work is done. Raises AtCapacityError."""
        if self._lagging() or self._in_flight >= self._limits.max_in_flight:
            self._refuse()
        self._in_flight += 1
        return Ticket(self)

    def _lagging(self) -> bool:
        return self._lag.lag_s > self._limits.max_loop_lag_s

    def _refuse(self) -> NoReturn:
        self._on_shed()
        # Jittered, so refused clients don't all come back in the same second.
        raise AtCapacityError(self._jitter())

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


class ShedWhileLagging:
    """The check at the door: while the event loop runs late, requests to the given paths
    get a 503 before their body is read or their key checked. Refused here, a request
    costs almost nothing; refused after parsing and auth, it would cost nearly as much as
    serving it, and the loop would stay saturated by the work done before refusing."""

    def __init__(self, app: ASGIApp, admissions: Mapping[str, Admission]) -> None:
        self._app = app
        self._admissions = admissions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        admission = self._admissions.get(scope["path"]) if scope["type"] == "http" else None
        try:
            if admission is not None:
                admission.refuse_if_lagging()
        except AtCapacityError as error:
            await at_capacity_response(error.retry_after_s)(scope, receive, send)
            return
        await self._app(scope, receive, send)


def at_capacity_response(retry_after_s: int) -> JSONResponse:
    # Our own capacity, not the provider's: 503 and when to come back, not 502.
    return JSONResponse(
        error_payload("The gateway is at capacity. Retry shortly.", "service_unavailable"),
        status_code=503,
        headers={"Retry-After": str(retry_after_s)},
    )
