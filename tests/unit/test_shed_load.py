"""Load shedding: past the in-flight limit, chat requests are refused at once."""

import threading
from collections.abc import AsyncIterator

import anyio
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

from llm_gateway.http.shed_load import ShedLoad
from tests.support.servers import serving


class Upstream:
    """A streaming endpoint that holds every response open until released."""

    def __init__(self) -> None:
        self.release = threading.Event()  # set from the test's thread
        self.started = 0

    async def chat(self, _: Request) -> Response:
        self.started += 1

        async def body() -> AsyncIterator[bytes]:
            yield b"data: first\n\n"
            await anyio.to_thread.run_sync(self.release.wait)
            yield b"data: [DONE]\n\n"

        return StreamingResponse(body())


def an_app(upstream: Upstream, shed: list[int]) -> ShedLoad:
    async def health(_: Request) -> Response:
        return PlainTextResponse("ok")

    inner = Starlette(
        routes=[Route("/chat", upstream.chat, methods=["POST"]), Route("/healthz", health)]
    )
    return ShedLoad(
        inner, max_in_flight=2, paths=frozenset({"/chat"}), on_shed=lambda: shed.append(1)
    )


def test_past_the_limit_a_chat_is_refused_at_once_and_counted() -> None:
    upstream, shed = Upstream(), list[int]()
    with (
        serving(an_app(upstream, shed)) as url,
        httpx.Client(base_url=url, timeout=5) as client,
        client.stream("POST", "/chat") as first,
        client.stream("POST", "/chat") as second,
    ):
        # Keep the iterators: httpx closes a stream whose line iterator is dropped.
        first_lines, second_lines = first.iter_lines(), second.iter_lines()
        next(first_lines), next(second_lines)  # two streams in flight
        refused = client.post("/chat")
        health = client.get("/healthz")  # other paths aren't limited
        upstream.release.set()

    assert (refused.status_code, refused.headers["retry-after"]) == (503, "1")
    assert refused.json()["error"]["type"] == "service_unavailable"
    assert health.status_code == 200
    assert (upstream.started, shed) == (2, [1])


def test_a_finished_stream_frees_its_place() -> None:
    upstream, shed = Upstream(), list[int]()
    upstream.release.set()
    with serving(an_app(upstream, shed)) as url, httpx.Client(base_url=url, timeout=5) as client:
        statuses = [client.post("/chat").status_code for _ in range(5)]

    assert statuses == [200] * 5
    assert shed == []
