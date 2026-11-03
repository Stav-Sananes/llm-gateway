"""A scriptable fake of the Anthropic Messages API, streaming its SSE event format.

Lets the real Anthropic SDK run in tests: healthy replies, error statuses, a stream that
fails mid-way with an `error` event, refusals, and a record of every request it got.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    404: "not_found_error",
    429: "rate_limit_error",
    500: "api_error",
    529: "overloaded_error",
}


@dataclass(frozen=True)
class FakeAnthropicScript:
    reply: tuple[str, ...] = ("Hello", " from", " fake", " Claude.")
    input_tokens: int = 9
    stop_reason: str = "end_turn"
    fail_status: int | None = None
    fail_message: str = "scripted failure"
    error_after_chunks: int | None = None  # send an overloaded `error` event after N deltas
    token_delay_s: float = 0.0


@dataclass
class FakeAnthropicLog:
    requests: list[dict[str, Any]] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)


def create_fake_anthropic(
    script: FakeAnthropicScript, log: FakeAnthropicLog | None = None
) -> FastAPI:
    app = FastAPI()
    request_log = log if log is not None else FakeAnthropicLog()

    @app.post("/v1/messages")
    async def messages(request: Request) -> Response:
        body: dict[str, Any] = await request.json()
        request_log.requests.append(body)
        request_log.headers.append(dict(request.headers))
        if script.fail_status is not None:
            return JSONResponse(
                _error(script.fail_status, script.fail_message), status_code=script.fail_status
            )
        return StreamingResponse(_events(script, body["model"]), media_type="text/event-stream")

    return app


async def _events(script: FakeAnthropicScript, model: str) -> AsyncIterator[str]:
    yield _event("message_start", {"type": "message_start", "message": _message(model, script)})
    yield _event(
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    )
    for index, text in enumerate(script.reply):
        if index == script.error_after_chunks:
            yield _event("error", _error(529, "Overloaded"))
            return
        await asyncio.sleep(script.token_delay_s)
        yield _event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        )
    yield _event("content_block_stop", {"type": "content_block_stop", "index": 0})
    yield _event(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": script.stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": len(script.reply)},
        },
    )
    yield _event("message_stop", {"type": "message_stop"})


def _message(model: str, script: FakeAnthropicScript) -> dict[str, Any]:
    return {
        "id": "msg_fake",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": script.input_tokens, "output_tokens": 1},
    }


def _error(status: int, message: str) -> dict[str, Any]:
    return {
        "type": "error",
        "error": {"type": _ERROR_TYPES.get(status, "api_error"), "message": message},
    }


def _event(name: str, payload: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(payload)}\n\n"
