"""A scriptable fake LLM speaking the OpenAI streaming protocol.

Used by acceptance tests now, and by load and chaos tests later: it gives a
fixed, known latency and can misbehave on demand (error status, broken stream).
"""

import asyncio
import hashlib
import json
import math
import re
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
    omit_done: bool = False  # end the stream cleanly but without the [DONE] line
    embedding_dims: int = 64
    embed_fail_status: int | None = None


@dataclass
class FakeLlmLog:
    requests: list[dict[str, Any]] = field(default_factory=list)
    embeddings: list[str] = field(default_factory=list)  # every text it was asked to embed


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

    @app.post("/v1/embeddings")
    async def embeddings(request: Request) -> Response:
        body: dict[str, Any] = await request.json()
        request_log.embeddings.append(body["input"])
        if script.embed_fail_status is not None:
            return JSONResponse(
                {"error": {"message": "down"}}, status_code=script.embed_fail_status
            )
        vector = bag_of_words(body["input"], script.embedding_dims)
        return JSONResponse({"object": "list", "data": [{"index": 0, "embedding": vector}]})

    return app


def bag_of_words(text: str, dims: int) -> list[float]:
    """A deterministic stand-in for an embedding model: the same words (in any case, with
    any punctuation) give the same vector; different words point elsewhere."""
    vector = [0.0] * dims
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % dims] += 1.0
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]


async def _stream(script: FakeLlmScript, model: str) -> AsyncIterator[str]:
    for index, token in enumerate(script.reply):
        if index == script.break_after_chunks:
            raise BrokenStreamError
        await asyncio.sleep(script.token_delay_s)
        yield _event(_chunk(model, {"content": token}, finish_reason=None))
    yield _event(_chunk(model, {}, finish_reason="stop"))
    yield _event(_usage_chunk(model, script))
    if not script.omit_done:
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
