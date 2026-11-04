"""Streaming errors after the first byte: always in-band, never a body cut off."""

import json

import pytest

from llm_gateway.chat.model import CompletionChunk
from llm_gateway.chat.ports import ChunkStream, ProviderRejectedError, ProviderUnavailableError
from llm_gateway.http.presenter import DONE_EVENT, ResponseIdentity, render_sse

pytestmark = pytest.mark.anyio


def failing_with(error: Exception) -> ChunkStream:
    async def rest() -> ChunkStream:
        yield CompletionChunk(content=" world")
        raise error

    return rest()


@pytest.mark.parametrize(
    "error",
    [
        ProviderUnavailableError("dropped"),
        ProviderRejectedError(400, "bad continuation"),  # anything else, too
        RuntimeError("a bug"),
    ],
    ids=["unavailable", "rejected", "unexpected"],
)
async def test_an_error_mid_stream_ends_with_an_error_event(error: Exception) -> None:
    events = [
        event
        async for event in render_sse(
            ResponseIdentity.new("m"),
            CompletionChunk(content="Hello"),
            failing_with(error),
            include_usage=False,
        )
    ]

    assert len(events) == 3 and DONE_EVENT not in events
    last = json.loads(events[-1].removeprefix("data: "))
    assert last["error"]["type"] == "upstream_error"
    assert "a bug" not in events[-1] and "bad continuation" not in events[-1]  # only logged
