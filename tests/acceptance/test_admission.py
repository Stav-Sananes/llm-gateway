"""Admission end to end: a busy gateway refuses with 503 and Retry-After, never 502, and
nothing a client does before its key is checked can take a place."""

import re
import socket
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from typing import Any
from urllib.parse import urlsplit

import httpx

from harness.fake_llm import FakeLlmScript, create_fake_llm
from llm_gateway.config import Settings
from llm_gateway.main import build_app
from tests.support.backing_services import new_api_key
from tests.support.servers import serving

QUESTION = {"model": "fake-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
SLOW = FakeLlmScript(reply=("a", "b", "c", "d"), token_delay_s=0.3)  # holds a stream ~1.2 s


@contextmanager
def a_gateway(
    services: Settings, upstream_script: FakeLlmScript = SLOW, **overrides: Any
) -> Iterator[httpx.Client]:
    api_key = new_api_key(services)
    with serving(create_fake_llm(upstream_script)) as upstream:
        settings = services.model_copy(
            update={"upstream_name": "fake", "upstream_base_url": f"{upstream}/v1"} | overrides
        )
        with (
            serving(build_app(settings)) as url,
            httpx.Client(
                base_url=url, headers={"Authorization": f"Bearer {api_key}"}, timeout=10
            ) as client,
        ):
            yield client


@contextmanager
def streaming(client: httpx.Client) -> Iterator[None]:
    """One chat streaming, mid-answer, for as long as the block runs."""
    with client.stream("POST", "/v1/chat/completions", json=QUESTION) as response:
        assert response.status_code == 200
        lines = response.iter_lines()  # keep it: dropping the iterator closes the stream
        next(lines)
        yield


def admitted_soon(client: httpx.Client, refusals: list[httpx.Response]) -> httpx.Response:
    """Ask until admitted: a place frees once the server has noticed the earlier stream
    end, which can be a moment after the client closed it."""
    deadline = time.monotonic() + 3
    while True:
        response = client.post("/v1/chat/completions", json=QUESTION)
        if response.status_code != 503 or time.monotonic() > deadline:
            return response
        refusals.append(response)
        time.sleep(0.05)


def refused(response: httpx.Response) -> bool:
    return response.status_code == 503 and response.headers.get("retry-after") in {"1", "2", "3"}


def test_past_the_in_flight_cap_a_chat_is_refused_with_retry_after(services: Settings) -> None:
    with a_gateway(services, max_concurrent_chats=1) as client:
        with streaming(client):
            second = client.post("/v1/chat/completions", json=QUESTION)
        late: list[httpx.Response] = []
        after = admitted_soon(client, late)  # the place is free again
        metrics = client.get("/metrics").text

    assert refused(second)
    assert all(refused(r) for r in late)
    assert f'gateway_requests_shed_total{{kind="chat"}} {1 + len(late)}.0' in metrics
    assert second.json()["error"]["type"] == "service_unavailable"
    assert after.status_code == 200


def test_clients_without_a_key_or_a_whole_body_hold_no_place(services: Settings) -> None:
    with a_gateway(services, max_concurrent_chats=1) as client, ExitStack() as sockets:
        address = urlsplit(str(client.base_url))
        for _ in range(5):  # never finish their bodies, and have no key
            sock = sockets.enter_context(socket.create_connection((address.hostname, address.port)))
            sock.sendall(
                b"POST /v1/chat/completions HTTP/1.1\r\nHost: gw\r\n"
                b"Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{"
            )
        answer = client.post("/v1/chat/completions", json=QUESTION)

    assert answer.status_code == 200


def test_with_every_connection_to_the_provider_busy_the_answer_is_503_not_502(
    services: Settings,
) -> None:
    with a_gateway(services, upstream_max_connections=1) as client, streaming(client):
        second = client.post("/v1/chat/completions", json=QUESTION)

    assert refused(second)


def test_a_request_that_fails_frees_its_place(services: Settings) -> None:
    broken = FakeLlmScript(fail_status=500)
    with a_gateway(services, broken, max_concurrent_chats=1) as client:
        statuses = [
            client.post("/v1/chat/completions", json=QUESTION).status_code for _ in range(3)
        ]

    assert statuses == [502, 502, 502]  # failed after admission, and never stuck at 503


def test_a_client_that_leaves_mid_stream_frees_its_place(services: Settings) -> None:
    with a_gateway(services, max_concurrent_chats=1) as client:
        with client.stream("POST", "/v1/chat/completions", json=QUESTION) as left:
            next(left.iter_lines())  # the iterator is dropped: httpx closes the stream
        answers = [admitted_soon(client, []) for _ in range(2)]

    assert [a.status_code for a in answers] == [200, 200]


def test_a_refused_key_holds_no_place(services: Settings) -> None:
    with a_gateway(services, max_concurrent_chats=1) as client:
        refused_keys = [
            client.post(
                "/v1/chat/completions", json=QUESTION, headers={"Authorization": "Bearer gw_bad"}
            ).status_code
            for _ in range(3)
        ]
        answer = client.post("/v1/chat/completions", json=QUESTION)

    assert refused_keys == [401, 401, 401]
    assert answer.status_code == 200


def test_a_non_streamed_answer_holds_its_place_until_it_is_complete(services: Settings) -> None:
    whole = QUESTION | {"stream": False}
    with a_gateway(services, max_concurrent_chats=1) as client:
        with ThreadPoolExecutor(1) as pool:
            first = pool.submit(client.post, "/v1/chat/completions", json=whole)
            time.sleep(0.5)  # admitted, and waiting on the slow upstream
            meanwhile = client.post("/v1/chat/completions", json=whole)
            first_answer = first.result()
        after = admitted_soon(client, [])

    assert refused(meanwhile)
    assert (first_answer.status_code, after.status_code) == (200, 200)


def test_while_the_loop_runs_late_requests_are_refused_before_their_key_is_checked(
    services: Settings,
) -> None:
    # Any lag at all is over this limit, so the door refuses as soon as it is measured.
    with a_gateway(services, max_loop_lag_s=1e-9) as client:
        deadline = time.monotonic() + 3
        while True:
            unknown_key = client.post(
                "/v1/chat/completions", json=QUESTION, headers={"Authorization": "Bearer gw_bad"}
            )
            if unknown_key.status_code != 401 or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        tool_call = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        metrics = client.get("/metrics").text

    # A 503, not a 401: refused at the door, so the refusal costs no key lookup.
    assert refused(unknown_key)
    assert refused(tool_call)
    assert 'gateway_requests_shed_total{kind="tool_call"} 1.0' in metrics


def test_the_gateway_measures_its_event_loop_lag(services: Settings) -> None:
    with a_gateway(services) as client:
        time.sleep(0.3)  # a few samples
        metrics = client.get("/metrics").text

    (lag,) = re.findall(r"^gateway_event_loop_lag_seconds (\S+)$", metrics, re.MULTILINE)
    assert 0 < float(lag) < 1  # measured: never set, it would read exactly 0
