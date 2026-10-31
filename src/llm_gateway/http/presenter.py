"""Renders domain chunks and completions in the OpenAI wire format."""

import json
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from llm_gateway.chat.model import Completion, CompletionChunk, Usage
from llm_gateway.chat.ports import ChunkStream, ProviderUnavailableError

DONE_EVENT = "data: [DONE]\n\n"


@dataclass(frozen=True)
class ResponseIdentity:
    id: str
    created: int
    model: str

    @classmethod
    def new(cls, model: str) -> "ResponseIdentity":
        return cls(id=f"chatcmpl-{uuid.uuid4().hex}", created=int(time.time()), model=model)

    def envelope(self, object_type: str) -> dict[str, Any]:
        return {"id": self.id, "object": object_type, "created": self.created, "model": self.model}


async def render_sse(
    identity: ResponseIdentity,
    first: CompletionChunk | None,
    rest: ChunkStream,
    *,
    include_usage: bool,
) -> AsyncIterator[str]:
    """Stream SSE events. Errors after the first byte can't change the HTTP status,
    so they are sent in-band as an error event, the way OpenAI does it."""
    usage: Usage | None = None
    try:
        if first is not None:
            usage = first.usage or usage
            yield _sse(chunk_payload(identity, first, is_first=True))
        async for chunk in rest:
            usage = chunk.usage or usage
            if chunk.content or chunk.finish_reason:
                yield _sse(chunk_payload(identity, chunk, is_first=False))
    except ProviderUnavailableError as error:
        yield _sse(error_payload(str(error), "upstream_error"))
        return
    finally:
        await rest.aclose()
    if include_usage and usage is not None:
        yield _sse(usage_payload(identity, usage))
    yield DONE_EVENT


def chunk_payload(
    identity: ResponseIdentity, chunk: CompletionChunk, *, is_first: bool
) -> dict[str, Any]:
    delta: dict[str, Any] = {"content": chunk.content}
    if is_first:
        delta["role"] = "assistant"
    return identity.envelope("chat.completion.chunk") | {
        "choices": [{"index": 0, "delta": delta, "finish_reason": chunk.finish_reason}],
    }


def usage_payload(identity: ResponseIdentity, usage: Usage) -> dict[str, Any]:
    return identity.envelope("chat.completion.chunk") | {
        "choices": [],
        "usage": _usage_fields(usage),
    }


def completion_payload(identity: ResponseIdentity, completion: Completion) -> dict[str, Any]:
    payload = identity.envelope("chat.completion") | {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": completion.content},
                "finish_reason": completion.finish_reason,
            }
        ],
    }
    if completion.usage is not None:
        payload["usage"] = _usage_fields(completion.usage)
    return payload


def error_payload(message: str, error_type: str, code: str | None = None) -> dict[str, Any]:
    return {"error": {"message": message, "type": error_type, "code": code}}


def _usage_fields(usage: Usage) -> dict[str, int]:
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
    }


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
