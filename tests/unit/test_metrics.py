import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_client import CollectorRegistry

from llm_gateway.metrics import (
    CircuitStates,
    GatewayMetrics,
    HttpMetrics,
    MeteredUsage,
    WorkerMetrics,
)
from llm_gateway.routing.breaker import CircuitBreaker
from llm_gateway.usage.model import Outcome
from llm_gateway.usage.worker import Drained
from tests.support.usage import RecordedUsage, a_usage_event

pytestmark = pytest.mark.anyio


def sample(metrics: GatewayMetrics, name: str, **labels: str) -> float | None:
    return metrics.registry.get_sample_value(name, labels)


@pytest.fixture
def metrics() -> GatewayMetrics:
    return GatewayMetrics(CollectorRegistry())


async def test_usage_is_counted_by_provider_and_outcome_and_passed_on(
    metrics: GatewayMetrics,
) -> None:
    inner = RecordedUsage()
    event = a_usage_event()

    await MeteredUsage(metrics, inner).record(event)

    assert inner.events == [event]
    ollama = {"provider": "ollama"}
    assert sample(metrics, "gateway_chat_requests_total", **ollama, outcome="completed") == 1
    assert sample(metrics, "gateway_chat_tokens_total", **ollama, kind="prompt") == 7
    assert sample(metrics, "gateway_chat_tokens_total", **ollama, kind="completion") == 5
    assert sample(metrics, "gateway_chat_time_to_first_chunk_seconds_sum", **ollama) == 0.25
    assert sample(metrics, "gateway_chat_duration_seconds_sum", **ollama) == 1.5


async def test_a_request_no_provider_served_is_counted_under_none(metrics: GatewayMetrics) -> None:
    failed = a_usage_event(
        provider=None, model=None, outcome=Outcome.FAILED, time_to_first_chunk_s=None
    )

    await MeteredUsage(metrics, RecordedUsage()).record(failed)

    assert sample(metrics, "gateway_chat_requests_total", provider="none", outcome="failed") == 1
    assert (
        sample(metrics, "gateway_chat_time_to_first_chunk_seconds_count", provider="none") is None
    )


def test_circuit_state_is_read_at_scrape_time(metrics: GatewayMetrics) -> None:
    breaker = CircuitBreaker("ollama", failure_threshold=1, reset_after_s=60)
    metrics.registry.register(CircuitStates([breaker]))
    assert sample(metrics, "gateway_circuit_state", provider="ollama") == 0

    breaker.record_failure()

    assert sample(metrics, "gateway_circuit_state", provider="ollama") == 2


def test_a_half_open_circuit_reads_1(metrics: GatewayMetrics) -> None:
    now = [0.0]
    breaker = CircuitBreaker("ollama", failure_threshold=1, reset_after_s=60, clock=lambda: now[0])
    metrics.registry.register(CircuitStates([breaker]))
    breaker.record_failure()
    now[0] = 61.0

    assert sample(metrics, "gateway_circuit_state", provider="ollama") == 1


async def test_http_responses_are_counted_by_known_path_and_status(
    metrics: GatewayMetrics,
) -> None:
    app = FastAPI()

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return JSONResponse({}, status_code=503)

    transport = httpx.ASGITransport(HttpMetrics(app, metrics))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        await client.get("/healthz")
        await client.get("/some/scanner/path/123")

    assert sample(metrics, "gateway_http_requests_total", path="/healthz", status="503") == 1
    assert sample(metrics, "gateway_http_request_duration_seconds_count", path="/healthz") == 1
    # Unknown paths share one label, so a scanner can't grow the metric without bound.
    assert sample(metrics, "gateway_http_requests_total", path="other", status="404") == 1


async def test_an_event_metrics_cannot_count_is_still_passed_on(metrics: GatewayMetrics) -> None:
    inner = RecordedUsage()
    odd = a_usage_event(prompt_tokens=-1)

    await MeteredUsage(metrics, inner).record(odd)

    assert inner.events == [odd]


async def test_an_app_that_raises_is_counted_as_a_500(metrics: GatewayMetrics) -> None:
    async def broken(scope: object, receive: object, send: object) -> None:
        raise RuntimeError("boom")

    transport = httpx.ASGITransport(HttpMetrics(broken, metrics), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        await client.get("/readyz")

    assert sample(metrics, "gateway_http_requests_total", path="/readyz", status="500") == 1


def test_worker_metrics_count_each_kind_of_result_apart() -> None:
    registry = CollectorRegistry()
    worker = WorkerMetrics(registry)

    worker.drained(Drained(written=3, dead_lettered=2, lost=1))
    worker.failed(RuntimeError("down"))
    worker.backlog(10, 4, 6)

    def value(name: str) -> float | None:
        return registry.get_sample_value(name)

    assert value("usage_worker_events_written_total") == 3
    assert value("usage_worker_events_dead_lettered_total") == 2
    assert value("usage_worker_events_lost_total") == 1
    assert value("usage_worker_failures_total") == 1
    assert (
        value("usage_stream_length"),
        value("usage_stream_pending"),
        value("usage_stream_lag"),
    ) == (10, 4, 6)
