"""Usage rows and metrics, end to end: gateway → Redis Stream → usage worker → Postgres."""

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy import select

from harness.fake_llm import FakeLlmLog, FakeLlmScript, create_fake_llm
from llm_gateway.config import Settings
from llm_gateway.main import build_app, build_engine
from llm_gateway.storage.tables import tenants, usage_events
from llm_gateway.storage.usage_store import PostgresUsageStore
from llm_gateway.storage.usage_stream import STREAM, RedisUsageQueue, decode
from llm_gateway.usage.model import UsageEvent
from llm_gateway.usage.worker import UsageWorker
from tests.support.backing_services import new_api_key
from tests.support.servers import serving

HELLO: dict[str, Any] = {"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]}


@contextmanager
def gateway(
    settings: Settings, script: FakeLlmScript | None = None, log: FakeLlmLog | None = None
) -> Iterator[httpx.Client]:
    api_key = new_api_key(settings)
    with serving(create_fake_llm(script or FakeLlmScript(), log)) as upstream_url:
        settings = settings.model_copy(update={"upstream_base_url": f"{upstream_url}/v1"})
        with (
            serving(build_app(settings)) as url,
            httpx.Client(base_url=url, headers={"Authorization": f"Bearer {api_key}"}) as client,
        ):
            yield client


def drain_usage_into_postgres(settings: Settings) -> list[dict[str, Any]]:
    """Run the usage worker once, as its process would, and read back usage_events."""

    async def drain() -> list[dict[str, Any]]:
        redis, engine = Redis.from_url(settings.redis_url, socket_timeout=5), build_engine(settings)
        try:
            queue = RedisUsageQueue(redis, "test-worker", block_ms=100)
            await queue.ensure_group()
            await UsageWorker(queue, PostgresUsageStore(engine)).drain_once()
            query = select(usage_events, tenants.c.name.label("tenant")).join(
                tenants, tenants.c.id == usage_events.c.tenant_id
            )
            async with engine.connect() as connection:
                return [dict(row._mapping) for row in await connection.execute(query)]
        finally:
            await redis.aclose()
            await engine.dispose()

    return asyncio.run(drain())


def queued_usage(settings: Settings) -> list[UsageEvent]:
    async def read() -> list[UsageEvent]:
        redis = Redis.from_url(settings.redis_url)
        try:
            entries = await redis.xrange(STREAM) or []
            raw = [fields[b"event"] for _, fields in entries if fields]
            return [decode(r.decode() if isinstance(r, bytes) else r) for r in raw]
        finally:
            await redis.aclose()

    return asyncio.run(read())


def test_a_chat_request_becomes_one_usage_row_for_its_tenant(services: Settings) -> None:
    with gateway(services) as client:
        response = client.post("/v1/chat/completions", json=HELLO)
    assert response.status_code == 200

    [row] = drain_usage_into_postgres(services)

    assert row["tenant"] == "acme"
    assert (row["provider"], row["model"], row["outcome"]) == ("ollama", "fake-model", "completed")
    assert (row["prompt_tokens"], row["completion_tokens"], row["charged_tokens"]) == (7, 5, 12)
    assert row["time_to_first_chunk_s"] <= row["duration_s"]


def test_metrics_show_requests_tokens_and_circuit_state(services: Settings) -> None:
    with gateway(services) as client:
        client.post("/v1/chat/completions", json=HELLO)
        client.post("/v1/chat/completions", json=HELLO, headers={"Authorization": "Bearer nope"})
        metrics = client.get("/metrics")

    assert metrics.status_code == 200
    text = metrics.text
    assert 'gateway_chat_requests_total{outcome="completed",provider="ollama"} 1.0' in text
    assert 'gateway_chat_tokens_total{kind="completion",provider="ollama"} 5.0' in text
    assert 'gateway_http_requests_total{path="/v1/chat/completions",status="401"} 1.0' in text
    assert 'gateway_circuit_state{provider="ollama"} 0.0' in text


def test_a_client_leaving_mid_stream_still_settles_and_records_usage(services: Settings) -> None:
    slow = FakeLlmScript(reply=tuple(f"word{i} " for i in range(50)), token_delay_s=0.2)
    with gateway(services, slow) as client:
        with client.stream(
            "POST", "/v1/chat/completions", json=HELLO | {"stream": True, "max_tokens": 500}
        ) as response:
            next(response.iter_lines())  # the first chunk arrived; now hang up
        deadline = time.monotonic() + 5
        while not queued_usage(services) and time.monotonic() < deadline:
            time.sleep(0.05)

    [event] = queued_usage(services)
    assert event.outcome == "interrupted"
    assert event.charged_tokens > 500  # the reservation: no usage was reported
    assert event.duration_s < 5  # ended at the hang-up, not after the whole 10 s answer


def test_a_model_name_that_would_poison_the_usage_queue_is_refused_at_the_edge(
    services: Settings,
) -> None:
    with gateway(services) as client:
        response = client.post("/v1/chat/completions", json=HELLO | {"model": "fake\u0000"})
        fine = client.post("/v1/chat/completions", json=HELLO)

    assert response.status_code == 400
    assert fine.status_code == 200
    assert [row["requested_model"] for row in drain_usage_into_postgres(services)] == ["fake-model"]
