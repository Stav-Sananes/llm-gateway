"""Shed load at the door: past a number of chat requests in flight, refuse new ones at once.

One process can only stream so many answers before its event loop falls behind. Then
everything slows down together: Redis calls hit their timeouts, connections to the
provider run out, and the provider's circuit opens although the provider is fine. A fast
503 with Retry-After for some requests keeps the rest fast. Each response is counted from
its first byte to its last, streaming included.
"""

from collections.abc import Callable

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from llm_gateway.http.presenter import error_payload

RETRY_AFTER_S = 1


class ShedLoad:
    def __init__(
        self,
        app: ASGIApp,
        max_in_flight: int,
        paths: frozenset[str],
        on_shed: Callable[[], None] = lambda: None,
    ) -> None:
        self._app = app
        self._max_in_flight = max_in_flight
        self._paths = paths
        self._on_shed = on_shed
        self._in_flight = 0  # one event loop: no lock needed

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] not in self._paths:
            await self._app(scope, receive, send)
            return
        if self._in_flight >= self._max_in_flight:
            self._on_shed()
            await _busy()(scope, receive, send)
            return
        self._in_flight += 1
        try:
            await self._app(scope, receive, send)
        finally:
            self._in_flight -= 1


def _busy() -> JSONResponse:
    return JSONResponse(
        error_payload("The gateway is at capacity. Retry shortly.", "service_unavailable"),
        status_code=503,
        headers={"Retry-After": str(RETRY_AFTER_S)},
    )
