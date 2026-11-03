"""Composition root: the only place that knows concrete classes and wires them together."""

import logging
import signal
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anthropic
import anyio
import httpx
import prometheus_client
import uvicorn
from fastapi import FastAPI
from prometheus_client import CollectorRegistry
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from llm_gateway import telemetry
from llm_gateway.audit.log import AuditLog
from llm_gateway.authz.cedar_engine import CedarPolicyEngine, load_policies
from llm_gateway.chat.ports import ChatProvider
from llm_gateway.chat.use_case import ChatCompletions
from llm_gateway.config import Settings
from llm_gateway.http.app import create_app
from llm_gateway.http.mcp_endpoint import build_mcp_app, build_mcp_server
from llm_gateway.limits.policies import FailOpenRateLimiter, UnlimitedBudget
from llm_gateway.limits.ports import TokenBudget
from llm_gateway.limits.token_bucket import InMemoryRateLimiter
from llm_gateway.metrics import CircuitStates, GatewayMetrics, MeteredUsage, WorkerMetrics
from llm_gateway.providers.anthropic import AnthropicProvider
from llm_gateway.providers.mcp_upstreams import HttpMcpUpstreams
from llm_gateway.providers.openai_compatible import OpenAICompatibleProvider
from llm_gateway.routing.breaker import CircuitBreaker, GuardedProvider
from llm_gateway.routing.router import Router, Target
from llm_gateway.storage.audit_store import PostgresAuditStore
from llm_gateway.storage.key_cache import RedisKeyCache
from llm_gateway.storage.rate_limiter import RedisRateLimiter
from llm_gateway.storage.tenant_store import PostgresTenantStore
from llm_gateway.storage.token_budget import RedisTokenBudget
from llm_gateway.storage.usage_store import PostgresUsageStore
from llm_gateway.storage.usage_stream import RedisUsageQueue, RedisUsageStream
from llm_gateway.tenancy.auth import Authenticator
from llm_gateway.tools.gateway import ToolGateway
from llm_gateway.tools.model import ToolLimits
from llm_gateway.usage.ports import UsageRecorder
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


def build_chat(
    settings: Settings, provider: ChatProvider, redis: Redis, usage: UsageRecorder
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
    return ChatCompletions(provider, rate_limiter, budget, settings.default_max_tokens, usage)


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
    tools = ToolGateway(
        HttpMcpUpstreams(settings.mcp_servers, settings.mcp_timeout_s),
        policy,
        AuditLog(PostgresAuditStore(engine)),
        ToolLimits(settings.mcp_max_argument_bytes, settings.mcp_max_result_bytes),
    )
    mcp_app, mcp_sessions = build_mcp_app(build_mcp_server(tools), authenticator)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with mcp_sessions.run():
            yield
        await upstream_client.aclose()
        if anthropic_client is not None:
            await anthropic_client.close()
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
    app = create_app(
        build_chat(settings, router, redis, usage),
        authenticator,
        breakers,
        lifespan=lifespan,
        mcp_app=mcp_app,
        metrics=metrics,
    )
    if tracer_provider is not None:
        telemetry.instrument(app, upstream_client)
    return app


def run(settings: Settings) -> None:
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port)


async def run_usage_worker(settings: Settings) -> None:
    """The usage worker process: drain the usage stream into Postgres until SIGTERM/SIGINT.

    Killed instead, nothing is lost: unacknowledged events are delivered again."""
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
            with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGINT) as signals:
                async for _ in signals:
                    logger.info("usage worker stopping")
                    tasks.cancel_scope.cancel()
                    break
    finally:
        await redis.aclose()
        await engine.dispose()
