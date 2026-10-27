"""End to end: real gateway, real fake upstream, real HTTP sockets, OpenAI wire format."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest

from harness.fake_llm import FakeLlmLog, FakeLlmScript, create_fake_llm
from llm_gateway.config import Settings
from llm_gateway.main import build_app
from tests.support.servers import serving, unused_local_url

HELLO = {"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]}


@contextmanager
def _gateway_to(upstream_url: str) -> Iterator[httpx.Client]:
    settings = Settings(upstream_name="fake", upstream_base_url=f"{upstream_url}/v1")
    with serving(build_app(settings)) as url, httpx.Client(base_url=url) as client:
        yield client


@contextmanager
def _gateway_with(script: FakeLlmScript, log: FakeLlmLog | None = None) -> Iterator[httpx.Client]:
    with serving(create_fake_llm(script, log)) as upstream_url, _gateway_to(upstream_url) as c:
        yield c


def _sse_events(response: httpx.Response) -> list[Any]:
    events: list[Any] = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            data = line.removeprefix("data: ")
            events.append(data if data == "[DONE]" else json.loads(data))
    return events


def _streamed_text(events: list[Any]) -> str:
    return "".join(
        event["choices"][0]["delta"].get("content", "")
        for event in events
        if isinstance(event, dict) and event.get("choices")
    )


def test_streams_openai_chunks_ending_with_done() -> None:
    with _gateway_with(FakeLlmScript()) as gateway:
        response = gateway.post("/v1/chat/completions", json=HELLO | {"stream": True})

    events = _sse_events(response)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert events[0]["object"] == "chat.completion.chunk"
    assert events[0]["choices"][0]["delta"]["role"] == "assistant"
    assert _streamed_text(events) == "Hello from the fake LLM."
    assert events[-2]["choices"][0]["finish_reason"] == "stop"
    assert events[-1] == "[DONE]"


def test_streams_usage_chunk_only_when_client_asks_for_it() -> None:
    with _gateway_with(FakeLlmScript(prompt_tokens=7)) as gateway:
        without_usage = _sse_events(
            gateway.post("/v1/chat/completions", json=HELLO | {"stream": True})
        )
        with_usage = _sse_events(
            gateway.post(
                "/v1/chat/completions",
                json=HELLO | {"stream": True, "stream_options": {"include_usage": True}},
            )
        )

    assert not any("usage" in event for event in without_usage if isinstance(event, dict))
    assert with_usage[-2]["usage"] == {
        "prompt_tokens": 7,
        "completion_tokens": 5,
        "total_tokens": 12,
    }


def test_returns_single_completion_when_client_does_not_stream() -> None:
    with _gateway_with(FakeLlmScript()) as gateway:
        response = gateway.post("/v1/chat/completions", json=HELLO)

    body = response.json()
    assert response.status_code == 200
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {
        "role": "assistant",
        "content": "Hello from the fake LLM.",
    }
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["total_tokens"] == 12


def test_always_streams_upstream_and_asks_for_usage() -> None:
    log = FakeLlmLog()
    with _gateway_with(FakeLlmScript(), log) as gateway:
        gateway.post("/v1/chat/completions", json=HELLO | {"max_tokens": 50})

    [upstream_request] = log.requests
    assert upstream_request["stream"] is True
    assert upstream_request["stream_options"] == {"include_usage": True}
    assert upstream_request["max_tokens"] == 50


@pytest.mark.parametrize("stream", [True, False])
def test_unreachable_upstream_is_a_502(stream: bool) -> None:
    with _gateway_to(unused_local_url()) as gateway:
        response = gateway.post("/v1/chat/completions", json=HELLO | {"stream": stream})

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "upstream_error"


def test_upstream_server_error_is_a_502() -> None:
    with _gateway_with(FakeLlmScript(fail_status=500)) as gateway:
        response = gateway.post("/v1/chat/completions", json=HELLO | {"stream": True})

    assert response.status_code == 502


def test_upstream_rejection_keeps_its_status_and_message() -> None:
    script = FakeLlmScript(fail_status=404, fail_message='model "nope" not found')
    with _gateway_with(script) as gateway:
        response = gateway.post("/v1/chat/completions", json=HELLO | {"stream": True})

    assert response.status_code == 404
    assert response.json()["error"] == {
        "message": 'model "nope" not found',
        "type": "invalid_request_error",
        "code": None,
    }


def test_stream_broken_midway_ends_with_in_band_error_event() -> None:
    with _gateway_with(FakeLlmScript(break_after_chunks=2)) as gateway:
        response = gateway.post("/v1/chat/completions", json=HELLO | {"stream": True})

    events = _sse_events(response)
    assert response.status_code == 200  # headers were already sent
    assert _streamed_text(events) == "Hello from"
    assert events[-1]["error"]["type"] == "upstream_error"


def test_invalid_body_is_a_400_in_openai_error_shape() -> None:
    with _gateway_with(FakeLlmScript()) as gateway:
        response = gateway.post("/v1/chat/completions", json={"model": "m", "messages": []})

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "messages" in response.json()["error"]["message"]
