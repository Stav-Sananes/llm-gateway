"""Prometheus metrics: what operators watch. (Per-tenant usage lives in Postgres instead.)

Labels stay bounded: provider names come from config, paths from a fixed list, and no
metric carries a tenant or a model name a client can choose.
"""

import time
from collections.abc import Awaitable, Callable, Iterable, Sequence

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from llm_gateway.routing.breaker import BreakerState, CircuitBreaker
from llm_gateway.usage.model import UsageEvent
from llm_gateway.usage.ports import UsageRecorder
from llm_gateway.usage.worker import Drained

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
NO_PROVIDER = "none"
_PATHS = frozenset({"/v1/chat/completions", "/mcp", "/healthz", "/readyz", "/metrics"})
_CIRCUIT_VALUES = {BreakerState.CLOSED: 0, BreakerState.HALF_OPEN: 1, BreakerState.OPEN: 2}
# Model latency, not web latency: first chunks take up to seconds, whole answers minutes.
_LLM_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)


class GatewayMetrics:
    """The gateway's metrics, on a registry of their own (one per app, so tests can build
    many apps in one process)."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self.registry = registry
        self.http_requests = Counter(
            "gateway_http_requests", "HTTP responses", ["path", "status"], registry=registry
        )
        self.http_duration = Histogram(
            "gateway_http_request_duration_seconds",
            "Time to the end of the response, streaming included",
            ["path"],
            buckets=_LLM_BUCKETS,
            registry=registry,
        )
        self.chat_requests = Counter(
            "gateway_chat_requests",
            "Chat requests by outcome",
            ["provider", "outcome"],
            registry=registry,
        )
        self.chat_tokens = Counter(
            "gateway_chat_tokens",
            "Tokens providers reported",
            ["provider", "kind"],
            registry=registry,
        )
        self.chat_duration = Histogram(
            "gateway_chat_duration_seconds",
            "Whole chat requests",
            ["provider"],
            buckets=_LLM_BUCKETS,
            registry=registry,
        )
        self.time_to_first_chunk = Histogram(
            "gateway_chat_time_to_first_chunk_seconds",
            "Until the first chunk",
            ["provider"],
            buckets=_LLM_BUCKETS,
            registry=registry,
        )
        self.usage_events_dropped = Counter(
            "gateway_usage_events_dropped",
            "Usage events lost: the queue was down",
            registry=registry,
        )

    def observe_usage(self, event: UsageEvent) -> None:
        provider = event.provider or NO_PROVIDER
        self.chat_requests.labels(provider, event.outcome.value).inc()
        self.chat_tokens.labels(provider, "prompt").inc(event.prompt_tokens)
        self.chat_tokens.labels(provider, "completion").inc(event.completion_tokens)
        self.chat_duration.labels(provider).observe(event.duration_s)
        if event.time_to_first_chunk_s is not None:
            self.time_to_first_chunk.labels(provider).observe(event.time_to_first_chunk_s)

    def render(self) -> bytes:
        return generate_latest(self.registry)


class MeteredUsage:
    """A UsageRecorder that counts each event, then passes it on."""

    def __init__(self, metrics: GatewayMetrics, inner: UsageRecorder) -> None:
        self._metrics = metrics
        self._inner = inner

    async def record(self, event: UsageEvent) -> None:
        self._metrics.observe_usage(event)
        await self._inner.record(event)


class CircuitStates(Collector):
    """gateway_circuit_state, read from the breakers at scrape time."""

    def __init__(self, breakers: Sequence[CircuitBreaker]) -> None:
        self._breakers = breakers

    def collect(self) -> Iterable[Metric]:
        gauge = GaugeMetricFamily(
            "gateway_circuit_state", "0 closed, 1 half-open, 2 open", labels=["provider"]
        )
        for breaker in self._breakers:
            gauge.add_metric([breaker.name], _CIRCUIT_VALUES[breaker.state])
        yield gauge


class HttpMetrics:
    """ASGI middleware counting responses by path and status."""

    def __init__(self, app: ASGIApp, metrics: GatewayMetrics) -> None:
        self._app = app
        self._metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        path = scope["path"] if scope["path"] in _PATHS else "other"
        status = 500  # if the app raises before responding, the server answers 500
        started = time.monotonic()

        async def counting_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self._app(scope, receive, counting_send)
        finally:
            self._metrics.http_requests.labels(path, str(status)).inc()
            self._metrics.http_duration.labels(path).observe(time.monotonic() - started)


def metrics_endpoint(metrics: GatewayMetrics) -> Callable[[Request], Awaitable[Response]]:
    async def endpoint(_: Request) -> Response:
        return Response(metrics.render(), media_type=CONTENT_TYPE)

    return endpoint


class WorkerMetrics:
    """The usage worker's WorkerObserver."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self.written = Counter(
            "usage_worker_events_written", "Usage events stored", registry=registry
        )
        self.dead_lettered = Counter(
            "usage_worker_events_dead_lettered", "Usage events parked", registry=registry
        )
        self.failures = Counter(
            "usage_worker_failures", "Batches not stored: queue or store down", registry=registry
        )

    def drained(self, result: Drained) -> None:
        self.written.inc(result.written)
        self.dead_lettered.inc(result.dead_lettered)

    def failed(self, error: Exception) -> None:
        self.failures.inc()
