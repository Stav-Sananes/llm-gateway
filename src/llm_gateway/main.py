"""Composition root: the only place that knows concrete classes and wires them together."""

import logging
import signal
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

import anthropic
import anyio
import httpx
import prometheus_client
import uvicorn
from fastapi import FastAPI
from prometheus_client import CollectorRegistry
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from llm_gateway import telemetry
from llm_gateway.approvals.hold import ApprovalHold, HoldSettings
from llm_gateway.audit.log import AuditLog
from llm_gateway.audit.switches import AuditedSwitches
from llm_gateway.authz.cedar_engine import CedarPolicyEngine, load_policies
from llm_gateway.cache.semantic import CacheBreakers, CacheSettings, SemanticCache
from llm_gateway.chat.ports import ChatProvider, ProviderSwitches
from llm_gateway.chat.use_case import NO_SWITCH_RECORDING, ChatCompletions
from llm_gateway.config import Settings
from llm_gateway.http.app import create_app
from llm_gateway.http.mcp_endpoint import build_mcp_app, build_mcp_server
from llm_gateway.limits.policies import FailOpenRateLimiter, UnlimitedBudget
from llm_gateway.limits.ports import TokenBudget
from llm_gateway.limits.token_bucket import InMemoryRateLimiter
from llm_gateway.metrics import (
    CacheMetrics,
    CircuitStates,
    GatewayMetrics,
    MeteredUsage,
    WorkerMetrics,
)
from llm_gateway.providers.anthropic import AnthropicProvider
from llm_gateway.providers.embeddings import OpenAICompatibleEmbedder
from llm_gateway.providers.mcp_upstreams import HttpMcpUpstreams
from llm_gateway.providers.openai_compatible import OpenAICompatibleProvider
from llm_gateway.routing.breaker import CircuitBreaker, GuardedProvider
from llm_gateway.routing.router import Router, Target
from llm_gateway.storage.approval_store import PostgresApprovalStore
from llm_gateway.storage.audit_store import PostgresAuditStore
from llm_gateway.storage.key_cache import RedisKeyCache
from llm_gateway.storage.qdrant_cache import QdrantAnswerStore
from llm_gateway.storage.rate_limiter import RedisRateLimiter
from llm_gateway.storage.tenant_store import PostgresTenantStore
from llm_gateway.storage.token_budget import RedisTokenBudget
from llm_gateway.storage.usage_store import PostgresUsageStore
from llm_gateway.storage.usage_stream import RedisUsageQueue, RedisUsageStream
from llm_gateway.tenancy.auth import Authenticator
from llm_gateway.tools.gateway import ToolGateway
from llm_gateway.tools.model import ToolLimits
from llm_gateway.usage.ports import UsageQueueUnavailableError, UsageRecorder
from llm_gateway.usage.worker import UsageWorker

logger = logging.getLogger(__name__)


def build_upstream_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=settings.upstream_base_url,
        timeout=httpx.Timeout(
            connect=settings.upstream_connect_timeout_s,
            read=settings.upstream_read_timeout_s,
            write=settings.upstream_connect_timeout_s,
            pool=settings.upstream_connect_timeout_s,
        ),
        limits=httpx.Limits(max_connections=settings.upstream_max_connections),
    )


def build_anthropic_client(settings: Settings) -> anthropic.AsyncAnthropic | None:
    if settings.anthropic_api_key is None:
        return None
    return anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value(),
        base_url=settings.anthropic_base_url,
        timeout=anthropic.Timeout(
            connect=settings.upstream_connect_timeout_s,
            read=settings.upstream_read_timeout_s,
            # Sending a request with images can take longer than connecting.
            write=settings.upstream_read_timeout_s,
            pool=settings.upstream_connect_timeout_s,
        ),
        # The router falls back to the next provider; SDK retries would only add latency.
        max_retries=0,
    )


def build_router(
    settings: Settings,
    upstream_client: httpx.AsyncClient,
    anthropic_client: anthropic.AsyncAnthropic | None,
) -> tuple[Router, list[CircuitBreaker]]:
    providers: dict[str, ChatProvider] = {
        settings.upstream_name: OpenAICompatibleProvider(upstream_client, settings.upstream_name)
    }
    if anthropic_client is not None:
        if "anthropic" in providers:
            raise ValueError("GATEWAY_UPSTREAM_NAME must not be 'anthropic'; that name is taken")
        providers["anthropic"] = AnthropicProvider(
            anthropic_client, settings.default_max_tokens, settings.anthropic_refusal_fallbacks
        )
    breakers = [
        CircuitBreaker(name, settings.breaker_failure_threshold, settings.breaker_reset_s)
        for name in providers
    ]
    guarded: dict[str, ChatProvider] = {
        b.name: GuardedProvider(providers[b.name], b) for b in breakers
    }
    routes = {
        model: [Target(t.provider, t.model) for t in targets]
        for model, targets in settings.routes.items()
    }
    return Router(guarded, routes, default_provider=settings.upstream_name), breakers


def build_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=settings.database_pool_size,
        # Bounded waits everywhere: pool checkout, connecting, and each statement.
        pool_timeout=settings.database_connect_timeout_s,
        pool_pre_ping=True,
        connect_args={
            "timeout": settings.database_connect_timeout_s,
            "command_timeout": settings.database_statement_timeout_s,
        },
    )


def build_redis(settings: Settings) -> Redis:
    return Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=settings.redis_timeout_s,
        socket_timeout=settings.redis_timeout_s,
    )


def build_cache(
    settings: Settings, metrics: GatewayMetrics
) -> tuple[SemanticCache | None, list[Callable[[], Awaitable[None]]]]:
    """The semantic cache, if enabled, and how to close its clients."""
    if not settings.cache_enabled:
        return None, []
    timeout = httpx.Timeout(settings.cache_timeout_s)
    embeddings_client = httpx.AsyncClient(
        base_url=settings.cache_embedding_base_url or settings.upstream_base_url, timeout=timeout
    )
    # No version check: it's a network call at construction, and the gateway must start
    # (and serve, uncached) without Qdrant.
    qdrant = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None,
        timeout=max(1, int(settings.cache_timeout_s)),
        check_compatibility=False,
    )
    cache = SemanticCache(
        OpenAICompatibleEmbedder(
            embeddings_client, settings.cache_embedding_model, settings.cache_embedding_dims
        ),
        QdrantAnswerStore(qdrant, settings.cache_embedding_dims),
        CacheBreakers(
            embedder=CircuitBreaker(
                "cache-embedder", settings.breaker_failure_threshold, settings.breaker_reset_s
            ),
            store=CircuitBreaker(
                "cache-store", settings.breaker_failure_threshold, settings.breaker_reset_s
            ),
        ),
        CacheSettings(
            similarity_threshold=settings.cache_similarity_threshold,
            ttl_s=settings.cache_ttl_s,
            timeout_s=settings.cache_timeout_s,
        ),
        observer=CacheMetrics(metrics, settings.cache_similarity_threshold),
    )
    return cache, [embeddings_client.aclose, qdrant.close]


def build_chat(
    settings: Settings,
    provider: ChatProvider,
    redis: Redis,
    usage: UsageRecorder,
    cache: SemanticCache | None = None,
    switches: ProviderSwitches = NO_SWITCH_RECORDING,
) -> ChatCompletions:
    burst, per_s = settings.rate_limit_burst, settings.rate_limit_per_s
    rate_limiter = FailOpenRateLimiter(
        shared=RedisRateLimiter(redis, burst, per_s), local=InMemoryRateLimiter(burst, per_s)
    )
    budget: TokenBudget = (
        UnlimitedBudget()
        if settings.token_budget_per_day is None
        else RedisTokenBudget(redis, settings.token_budget_per_day)
    )
    return ChatCompletions(
        provider,
        rate_limiter,
        budget,
        settings.default_max_tokens,
        usage,
        cache=cache,
        switches=switches,
    )


def build_app(settings: Settings) -> FastAPI:
    tracer_provider = telemetry.install_tracer_provider() if settings.tracing_enabled else None
    upstream_client = build_upstream_client(settings)
    anthropic_client = build_anthropic_client(settings)
    router, breakers = build_router(settings, upstream_client, anthropic_client)
    engine = build_engine(settings)
    redis = build_redis(settings)
    authenticator = Authenticator(
        PostgresTenantStore(engine), RedisKeyCache(redis, settings.key_cache_ttl_s)
    )

    # Loaded now, so a broken policy file stops the gateway from starting.
    policy = CedarPolicyEngine(load_policies(settings.policy_file))
    audit = AuditLog(PostgresAuditStore(engine))
    switches = AuditedSwitches(audit)
    tools = ToolGateway(
        HttpMcpUpstreams(settings.mcp_servers, settings.mcp_timeout_s),
        policy,
        audit,
        ToolLimits(settings.mcp_max_argument_bytes, settings.mcp_max_result_bytes),
        hold=ApprovalHold(
            PostgresApprovalStore(engine),
            HoldSettings(
                timeout_s=settings.approval_timeout_s,
                poll_s=settings.approval_poll_s,
                max_pending_per_tenant=settings.approval_max_pending_per_tenant,
            ),
        ),
    )
    mcp_app, mcp_sessions = build_mcp_app(build_mcp_server(tools), authenticator)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp_sessions.run(), anyio.create_task_group() as background:
            background.start_soon(switches.run)  # audits provider switches off the response path
            if cache is not None:
                background.start_soon(cache.warm_up)  # load the embedding model before traffic
                background.start_soon(cache.run_writer)  # stores answers off the response path
            yield
            background.cancel_scope.cancel()
        await upstream_client.aclose()
        if anthropic_client is not None:
            await anthropic_client.close()
        for close in cache_clients:
            await close()
        await redis.aclose()
        await engine.dispose()
        if tracer_provider is not None:
            tracer_provider.shutdown()

    metrics = GatewayMetrics(CollectorRegistry())
    metrics.registry.register(CircuitStates(breakers))
    usage = MeteredUsage(
        metrics,
        RedisUsageStream(
            redis, settings.usage_stream_max_len, on_dropped=metrics.usage_events_dropped.inc
        ),
    )
    cache, cache_clients = build_cache(settings, metrics)
    app = create_app(
        build_chat(settings, router, redis, usage, cache, switches),
        authenticator,
        breakers,
        lifespan=lifespan,
        mcp_app=mcp_app,
        metrics=metrics,
        max_request_bytes=settings.max_request_bytes,
    )
    if tracer_provider is not None:
        telemetry.instrument(app, upstream_client)
    return app


def run(settings: Settings) -> None:
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port)


async def run_usage_worker(settings: Settings) -> None:
    """The usage worker process: drain the usage stream into Postgres until SIGTERM/SIGINT.

    Killed instead, nothing is lost: unacknowledged events are delivered again."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    registry = CollectorRegistry()
    observer = WorkerMetrics(registry)
    prometheus_client.start_http_server(settings.usage_worker_metrics_port, registry=registry)
    block_s = 5
    # Its own client: a blocking read waits block_s, past the gateway's short socket timeout.
    redis = Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=settings.redis_timeout_s,
        socket_timeout=block_s + 5,
    )
    engine = build_engine(settings)
    queue = RedisUsageQueue(redis, consumer=socket.gethostname(), block_ms=block_s * 1000)
    worker = UsageWorker(queue, PostgresUsageStore(engine), observer=observer)
    try:
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(worker.run)
            tasks.start_soon(_report_backlog, queue, observer)
            with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
                async for _ in signals:
                    logger.info("usage worker stopping")
                    tasks.cancel_scope.cancel()
                    break
    finally:
        await redis.aclose()
        await engine.dispose()


async def _report_backlog(queue: RedisUsageQueue, metrics: WorkerMetrics) -> None:
    while True:
        # When Redis is down, the worker loop already reports it.
        with suppress(UsageQueueUnavailableError):
            metrics.backlog(*await queue.backlog())
        await anyio.sleep(15)
