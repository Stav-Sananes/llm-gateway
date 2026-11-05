"""Usage rows and metrics, end to end: gateway → Redis Stream → usage worker → Postgres."""

import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from prometheus_client import CollectorRegistry, generate_latest
from redis.asyncio import Redis
from sqlalchemy import select

from harness.fake_llm import FakeLlmLog, FakeLlmScript, create_fake_llm
from llm_gateway.config import Settings
from llm_gateway.main import build_app, build_engine
from llm_gateway.metrics import WorkerMetrics
from llm_gateway.storage.tables import tenants, usage_events
from llm_gateway.storage.usage_store import PostgresUsageStore
from llm_gateway.storage.usage_stream import STREAM, RedisUsageQueue, RedisUsageStream, decode
from llm_gateway.usage.model import UsageEvent
from llm_gateway.usage.worker import UsageWorker
from tests.support.backing_services import new_api_key
from tests.support.servers import serving, unused_local_url
from tests.support.usage import a_usage_event

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


def test_a_usage_event_redis_cannot_take_is_counted_as_dropped(services: Settings) -> None:
    # Redis down: auth falls back to Postgres, the rate limit to a local bucket, and with
    # budgets off the request goes through; only its usage event is lost.
    api_key = new_api_key(services)
    down = services.model_copy(
        update={
            "redis_url": unused_local_url().replace("http", "redis"),
            "token_budget_per_day": None,
        }
    )
    with serving(create_fake_llm(FakeLlmScript())) as upstream_url:
        settings = down.model_copy(update={"upstream_base_url": f"{upstream_url}/v1"})
        with (
            serving(build_app(settings)) as url,
            httpx.Client(base_url=url, headers={"Authorization": f"Bearer {api_key}"}) as client,
        ):
            assert client.post("/v1/chat/completions", json=HELLO).status_code == 200
            metrics = client.get("/metrics").text

    assert "gateway_usage_events_dropped_total 1.0" in metrics


DASHBOARD = Path(__file__).parents[2] / "deploy/grafana/dashboards/gateway.json"
_METRIC_NAME = re.compile(r"\b(?:gateway|usage)_[a-z_]+")
_SUFFIXES = ("_bucket", "_total", "_count", "_sum")


def dashboard_metric_names() -> set[str]:
    exprs = [
        target["expr"]
        for panel in json.loads(DASHBOARD.read_text())["panels"]
        for target in panel.get("targets", [])
        if "expr" in target
    ]
    names = {name for expr in exprs for name in _METRIC_NAME.findall(expr)}
    assert names, "positive control: the dashboard queries some metrics"
    return {next((n.removesuffix(s) for s in _SUFFIXES if n.endswith(s)), n) for n in names}


def free_port() -> int:
    return int(unused_local_url().rsplit(":", 1)[1])


@contextmanager
def usage_worker_process(settings: Settings, metrics_port: int) -> Iterator[subprocess.Popen[str]]:
    env = os.environ | {
        "GATEWAY_DATABASE_URL": settings.database_url,
        "GATEWAY_REDIS_URL": settings.redis_url,
        "GATEWAY_USAGE_WORKER_METRICS_PORT": str(metrics_port),
    }
    # The installed command, as Compose runs it. A fixed argv: no shell, no untrusted input.
    cli = [str(Path(sys.executable).parent / "llm-gateway"), "usage-worker"]
    process = subprocess.Popen(  # noqa: S603
        cli, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def wait_for(check: Any, timeout_s: float = 10) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            if result := check():
                return result
        time.sleep(0.1)
    raise AssertionError("timed out")


def usage_rows(settings: Settings) -> int:
    async def count() -> int:
        engine = build_engine(settings)
        try:
            async with engine.connect() as connection:
                return len((await connection.execute(select(usage_events.c.event_id))).all())
        finally:
            await engine.dispose()

    return asyncio.run(count())


def test_the_usage_worker_process_stores_events_reports_metrics_and_stops_on_sigterm(
    services: Settings,
) -> None:
    async def record() -> None:
        redis = Redis.from_url(services.redis_url)
        try:
            await RedisUsageStream(redis).record(a_usage_event())
        finally:
            await redis.aclose()

    port = free_port()
    with usage_worker_process(services, port) as worker:
        asyncio.run(record())
        wait_for(lambda: usage_rows(services) == 1)
        metrics = wait_for(lambda: httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=2).text)
        worker.send_signal(signal.SIGTERM)
        exit_code = worker.wait(timeout=10)

    assert "usage_worker_events_written_total 1.0" in metrics
    assert exit_code == 0, worker.stdout.read() if worker.stdout else ""


def test_every_metric_the_dashboard_queries_exists(services: Settings) -> None:
    with gateway(services) as client:
        client.post("/v1/chat/completions", json=HELLO)
        exposed = client.get("/metrics").text
    worker_registry = CollectorRegistry()
    WorkerMetrics(worker_registry)
    exposed += generate_latest(worker_registry).decode()

    missing = {name for name in dashboard_metric_names() if name not in exposed}

    assert missing == set()


def test_the_gateway_refuses_a_body_over_its_limit(services: Settings) -> None:
    small = services.model_copy(update={"max_request_bytes": 1000})
    big = HELLO | {"messages": [{"role": "user", "content": "x" * 2000}]}
    with gateway(small) as client:
        response = client.post("/v1/chat/completions", json=big)

    assert response.status_code == 413
    assert queued_usage(services) == []  # refused before anything ran


def test_the_gateway_refuses_a_chunked_body_over_its_limit(services: Settings) -> None:
    # No Content-Length: the body is counted as it arrives and cut off. FastAPI would answer
    # the cut-off body with its own 400, so this checks the 413 wins on the real endpoint.
    small = services.model_copy(update={"max_request_bytes": 1000})
    big = json.dumps(HELLO | {"messages": [{"role": "user", "content": "x" * 2000}]}).encode()
    with gateway(small) as client:
        response = client.post(
            "/v1/chat/completions",
            content=iter([big[:500], big[500:]]),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "request_too_large"
    assert queued_usage(services) == []
