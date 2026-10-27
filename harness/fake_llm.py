"""A scriptable fake LLM speaking the OpenAI streaming protocol.

Used by acceptance tests now, and by load and chaos tests later: it gives a
fixed, known latency and can misbehave on demand (error status, broken stream).
"""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse


@dataclass(frozen=True)
class FakeLlmScript:
    reply: tuple[str, ...] = ("Hello", " from", " the", " fake", " LLM.")
    prompt_tokens: int = 7
    token_delay_s: float = 0.0
    fail_status: int | None = None
    fail_message: str = "scripted failure"
    break_after_chunks: int | None = None


@dataclass
class FakeLlmLog:
    requests: list[dict[str, Any]] = field(default_factory=list)


class BrokenStreamError(Exception):
    """Raised mid-stream so the server drops the connection without finishing the body."""


def create_fake_llm(script: FakeLlmScript, log: FakeLlmLog | None = None) -> FastAPI:
    app = FastAPI()
    request_log = log if log is not None else FakeLlmLog()

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Response:
        body: dict[str, Any] = await request.json()
        request_log.requests.append(body)
        if script.fail_status is not None:
            return JSONResponse(
                {"error": {"message": script.fail_message}}, status_code=script.fail_status
            )
        return StreamingResponse(_stream(script, body["model"]), media_type="text/event-stream")

    return app


async def _stream(script: FakeLlmScript, model: str) -> AsyncIterator[str]:
    for index, token in enumerate(script.reply):
        if index == script.break_after_chunks:
            raise BrokenStreamError
        await asyncio.sleep(script.token_delay_s)
        yield _event(_chunk(model, {"content": token}, finish_reason=None))
    yield _event(_chunk(model, {}, finish_reason="stop"))
    yield _event(_usage_chunk(model, script))
    yield "data: [DONE]\n\n"


def _chunk(model: str, delta: dict[str, str], finish_reason: str | None) -> dict[str, Any]:
    return {
        "id": "fake-1",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _usage_chunk(model: str, script: FakeLlmScript) -> dict[str, Any]:
    return {
        "id": "fake-1",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [],
        "usage": {
            "prompt_tokens": script.prompt_tokens,
            "completion_tokens": len(script.reply),
            "total_tokens": script.prompt_tokens + len(script.reply),
        },
    }


def _event(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"
