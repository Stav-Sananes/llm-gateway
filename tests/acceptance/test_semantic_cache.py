"""The semantic cache end to end: gateway, fake LLM (chat + embeddings), real Qdrant."""

import asyncio
import json
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

import httpx
import pytest
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis

from harness.fake_llm import FakeLlmLog, FakeLlmScript, create_fake_llm
from llm_gateway.config import Settings
from llm_gateway.main import build_app
from llm_gateway.storage.qdrant_cache import COLLECTION
from llm_gateway.storage.usage_stream import STREAM, decode
from tests.support.backing_services import new_api_key
from tests.support.servers import serving, unused_local_url

QUESTION: dict[str, Any] = {
    "model": "fake-model",
    "temperature": 0,
    "messages": [{"role": "user", "content": "What is the capital of France?"}],
}
REWORDED = QUESTION | {"messages": [{"role": "user", "content": "what is the capital of france"}]}


@pytest.fixture
def cached_settings(services: Settings, qdrant_url: str) -> Settings:
    async def empty() -> None:
        client = AsyncQdrantClient(url=qdrant_url, check_compatibility=False)
        try:
            if await client.collection_exists(COLLECTION):
                await client.delete_collection(COLLECTION)
        finally:
            await client.close()

    asyncio.run(empty())
    return services.model_copy(
        update={"cache_enabled": True, "qdrant_url": qdrant_url, "cache_embedding_dims": 64}
    )


Gateway = Callable[..., AbstractContextManager[tuple[httpx.Client, FakeLlmLog]]]


@pytest.fixture
def gateway() -> Gateway:
    @contextmanager
    def running(
        settings: Settings, api_key: str | None = None
    ) -> Iterator[tuple[httpx.Client, FakeLlmLog]]:
        key = api_key or new_api_key(settings)
        log = FakeLlmLog()
        with serving(create_fake_llm(FakeLlmScript(embedding_dims=64), log)) as upstream:
            configured = settings.model_copy(update={"upstream_base_url": f"{upstream}/v1"})
            with (
                serving(build_app(configured)) as url,
                httpx.Client(base_url=url, headers={"Authorization": f"Bearer {key}"}) as client,
            ):
                yield client, log

    return running


def wait_until_cached(settings: Settings, count: int = 1) -> None:
    """Answers are written without waiting for Qdrant to index them."""

    async def points() -> int:
        client = AsyncQdrantClient(url=settings.qdrant_url, check_compatibility=False)
        try:
            if not await client.collection_exists(COLLECTION):
                return 0
            return (await client.count(COLLECTION, exact=True)).count
        finally:
            await client.close()

    deadline = time.monotonic() + 5
    while asyncio.run(points()) < count:
        assert time.monotonic() < deadline, "the answer was never cached"
        time.sleep(0.05)


def streamed_text(response: httpx.Response) -> str:
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    return "".join(e["choices"][0]["delta"].get("content", "") for e in events if e["choices"])


def ask(client: httpx.Client, body: dict[str, Any], **headers: str) -> httpx.Response:
    response = client.post("/v1/chat/completions", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response


def test_a_question_asked_again_in_other_words_is_answered_from_the_cache(
    cached_settings: Settings, gateway: Gateway
) -> None:
    with gateway(cached_settings) as (client, log):
        first = ask(client, QUESTION)
        wait_until_cached(cached_settings)
        again = ask(client, REWORDED)

    assert first.headers["x-gateway-cache"] == "miss"
    assert again.headers["x-gateway-cache"] == "hit"
    assert again.json()["choices"][0]["message"]["content"] == "Hello from the fake LLM."
    assert len(log.requests) == 1  # the model was asked once
    assert again.json()["usage"]["total_tokens"] == 0


def test_a_streamed_answer_can_come_from_the_cache(
    cached_settings: Settings, gateway: Gateway
) -> None:
    with gateway(cached_settings) as (client, log):
        ask(client, QUESTION)
        wait_until_cached(cached_settings)
        streamed = ask(client, QUESTION | {"stream": True})

    assert streamed.headers["x-gateway-cache"] == "hit"
    assert streamed_text(streamed) == "Hello from the fake LLM."
    assert len(log.requests) == 1


def test_one_tenants_answers_are_never_served_to_another(
    cached_settings: Settings, gateway: Gateway
) -> None:
    globex = new_api_key(cached_settings, "globex")
    with gateway(cached_settings) as (client, log):
        ask(client, QUESTION)
        wait_until_cached(cached_settings)
        other = client.post(
            "/v1/chat/completions", json=QUESTION, headers={"Authorization": f"Bearer {globex}"}
        )

    assert other.headers["x-gateway-cache"] == "miss"
    assert len(log.requests) == 2


def test_requests_without_a_low_temperature_skip_the_cache(
    cached_settings: Settings, gateway: Gateway
) -> None:
    unset = {k: v for k, v in QUESTION.items() if k != "temperature"}
    with gateway(cached_settings) as (client, log):
        responses = [
            ask(client, unset),
            ask(client, unset),
            ask(client, QUESTION | {"temperature": 0.7}),
        ]

    assert [r.headers["x-gateway-cache"] for r in responses] == ["miss"] * 3
    assert len(log.requests) == 3
    assert log.embeddings == []  # not even embedded


def test_cache_control_no_cache_gets_a_fresh_answer(
    cached_settings: Settings, gateway: Gateway
) -> None:
    with gateway(cached_settings) as (client, log):
        ask(client, QUESTION)
        wait_until_cached(cached_settings)
        fresh = ask(client, QUESTION, **{"Cache-Control": "no-cache"})

    assert fresh.headers["x-gateway-cache"] == "miss"
    assert len(log.requests) == 2


def test_with_qdrant_down_every_request_still_gets_an_answer(
    cached_settings: Settings, gateway: Gateway
) -> None:
    down = cached_settings.model_copy(update={"qdrant_url": unused_local_url()})
    with gateway(down) as (client, log):
        responses = [ask(client, QUESTION) for _ in range(8)]
        metrics = client.get("/metrics").text

    assert all(r.headers["x-gateway-cache"] == "miss" for r in responses)
    assert len(log.requests) == 8
    assert 'gateway_cache_lookups_total{result="unavailable"} 8.0' in metrics
    # The circuit opened: after the threshold, lookups stop trying to embed at all.
    assert len(log.embeddings) == cached_settings.breaker_failure_threshold


def test_a_cache_hit_is_recorded_as_free_usage_from_the_cache(
    cached_settings: Settings, gateway: Gateway
) -> None:
    with gateway(cached_settings) as (client, _):
        ask(client, QUESTION)
        wait_until_cached(cached_settings)
        ask(client, QUESTION)
        metrics = client.get("/metrics").text

    async def queued() -> list[Any]:
        redis = Redis.from_url(cached_settings.redis_url)
        try:
            raw = [f[b"event"] for _, f in await redis.xrange(STREAM) or [] if f]
            return [decode(r.decode() if isinstance(r, bytes) else r) for r in raw]
        finally:
            await redis.aclose()

    miss, hit = asyncio.run(queued())
    assert (miss.provider, hit.provider) == ("ollama", "cache")
    assert (miss.charged_tokens, hit.charged_tokens) == (12, 0)
    assert 'gateway_cache_lookups_total{result="hit"} 1.0' in metrics
    assert "gateway_cache_similarity_threshold 0.95" in metrics
