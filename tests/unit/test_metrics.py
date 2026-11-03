import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_client import CollectorRegistry

from llm_gateway.metrics import CircuitStates, GatewayMetrics, HttpMetrics, MeteredUsage
from llm_gateway.routing.breaker import CircuitBreaker
from llm_gateway.usage.model import Outcome
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
    # Unknown paths share one label, so a scanner can't grow the metric without bound.
    assert sample(metrics, "gateway_http_requests_total", path="other", status="404") == 1
