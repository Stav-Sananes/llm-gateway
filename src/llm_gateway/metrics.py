"""Prometheus metrics: what operators watch. (Per-tenant usage lives in Postgres instead.)

Labels stay bounded: provider names come from config, paths from a fixed list, and no
metric carries a tenant or a model name a client can choose.
"""

import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from llm_gateway.cache.semantic import LookupResult
from llm_gateway.routing.breaker import BreakerState, CircuitBreaker
from llm_gateway.usage.model import Outcome, UsageEvent
from llm_gateway.usage.ports import UsageRecorder
from llm_gateway.usage.worker import Drained

logger = logging.getLogger(__name__)

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
        self.chat_resumes = Counter(
            "gateway_chat_resumes",
            "Answers a provider began, died during, and another provider continued",
            ["provider"],
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
        self.cache_lookups = Counter(
            "gateway_cache_lookups",
            "Semantic cache lookups by result",
            ["result"],
            registry=registry,
        )
        self.cache_hit_score = Histogram(
            "gateway_cache_hit_score",
            "Similarity of the questions on a cache hit",
            buckets=(0.9, 0.92, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99, 0.995, 1.0),
            registry=registry,
        )
        self.cache_writes_dropped = Counter(
            "gateway_cache_writes_dropped",
            "Answers not cached: the write queue was full",
            registry=registry,
        )
        self.cache_threshold = Gauge(
            "gateway_cache_similarity_threshold",
            "Minimum similarity for a cache hit",
            registry=registry,
        )
        self.usage_events_dropped = Counter(
            "gateway_usage_events_dropped",
            "Usage events lost: the queue was down",
            registry=registry,
        )

    def observe_usage(self, event: UsageEvent) -> None:
        provider = event.provider or NO_PROVIDER
        self.chat_tokens.labels(provider, "prompt").inc(event.prompt_tokens)
        self.chat_tokens.labels(provider, "completion").inc(event.completion_tokens)
        if event.outcome is Outcome.RESUMED:
            # Another event follows for the same request: count the request once, there.
            self.chat_resumes.labels(provider).inc()
            return
        self.chat_requests.labels(provider, event.outcome.value).inc()
        self.chat_duration.labels(provider).observe(event.duration_s)
        if event.time_to_first_chunk_s is not None:
            self.time_to_first_chunk.labels(provider).observe(event.time_to_first_chunk_s)

    def render(self) -> bytes:
        return generate_latest(self.registry)


class MeteredUsage:
    """A UsageRecorder that counts each event, then passes it on. Never raises."""

    def __init__(self, metrics: GatewayMetrics, inner: UsageRecorder) -> None:
        self._metrics = metrics
        self._inner = inner

    async def record(self, event: UsageEvent) -> None:
        try:
            self._metrics.observe_usage(event)
        except ValueError:  # e.g. a negative count from a misbehaving upstream
            logger.warning("usage event %s not counted in metrics", event.event_id)
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
        self.lost = Counter(
            "usage_worker_events_lost",
            "Usage events trimmed before they were stored",
            registry=registry,
        )
        self.failures = Counter(
            "usage_worker_failures", "Batches not stored: queue or store down", registry=registry
        )
        # Alert on these before the stream reaches its cap and starts trimming.
        self.stream_length = Gauge(
            "usage_stream_length", "Entries in the usage stream", registry=registry
        )
        self.pending = Gauge(
            "usage_stream_pending", "Delivered, not yet acknowledged", registry=registry
        )
        self.lag = Gauge("usage_stream_lag", "Never delivered yet", registry=registry)

    def drained(self, result: Drained) -> None:
        self.written.inc(result.written)
        self.dead_lettered.inc(result.dead_lettered)
        self.lost.inc(result.lost)

    def backlog(self, length: int, pending: int, lag: int) -> None:
        self.stream_length.set(length)
        self.pending.set(pending)
        self.lag.set(lag)

    def failed(self, error: Exception) -> None:
        self.failures.inc()


class CacheMetrics:
    """The semantic cache's CacheObserver."""

    def __init__(self, metrics: GatewayMetrics, threshold: float) -> None:
        self._metrics = metrics
        metrics.cache_threshold.set(threshold)

    def looked_up(self, result: LookupResult, score: float | None) -> None:
        self._metrics.cache_lookups.labels(result.value).inc()
        if score is not None:
            self._metrics.cache_hit_score.observe(score)

    def write_dropped(self) -> None:
        self._metrics.cache_writes_dropped.inc()
