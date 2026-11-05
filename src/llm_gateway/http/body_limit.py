"""Refuse request bodies over a size limit, before anything reads them into memory."""

import contextlib

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from llm_gateway.http.presenter import error_payload


class BodyTooLargeError(Exception):
    def __init__(self, max_bytes: int) -> None:
        super().__init__(f"request body is over {max_bytes} bytes")
        self.max_bytes = max_bytes


def too_large_response(max_bytes: int) -> JSONResponse:
    return JSONResponse(
        error_payload(
            f"Request body is over {max_bytes} bytes.", "invalid_request_error", "request_too_large"
        ),
        status_code=413,
    )


class BodyLimit:
    """413 when Content-Length is over the limit. A body without Content-Length (chunked)
    is counted as it arrives and cut off when it goes over: receive() raises
    BodyTooLargeError, and the 413 replaces whatever the app would have answered."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self._max_bytes):
            await self._too_large(scope, receive, send)
            return
        received = 0
        cut_off = False
        started = False

        async def counting_receive() -> Message:
            nonlocal received, cut_off
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._max_bytes:
                    cut_off = True
                    raise BodyTooLargeError(self._max_bytes)
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if cut_off:
                # Whatever the app answers to a body it never got whole (FastAPI makes it a
                # 400 "error parsing the body", Starlette a 500) is replaced by the 413.
                return
            started = started or message["type"] == "http.response.start"
            await send(message)

        with contextlib.suppress(BodyTooLargeError):
            await self._app(scope, counting_receive, guarded_send)
        if cut_off and not started:
            await self._too_large(scope, receive, send)

    async def _too_large(self, scope: Scope, receive: Receive, send: Send) -> None:
        await too_large_response(self._max_bytes)(scope, receive, send)
