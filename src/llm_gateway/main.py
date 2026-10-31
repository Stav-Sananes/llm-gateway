"""Composition root: the only place that knows concrete classes and wires them together."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from llm_gateway import telemetry
from llm_gateway.chat.use_case import ChatCompletions
from llm_gateway.config import Settings
from llm_gateway.http.app import create_app
from llm_gateway.providers.openai_compatible import OpenAICompatibleProvider


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


def build_app(settings: Settings) -> FastAPI:
    tracer_provider = telemetry.install_tracer_provider() if settings.tracing_enabled else None
    upstream_client = build_upstream_client(settings)
    provider = OpenAICompatibleProvider(upstream_client, settings.upstream_name)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await upstream_client.aclose()
        if tracer_provider is not None:
            tracer_provider.shutdown()

    app = create_app(ChatCompletions(provider), lifespan=lifespan)
    if tracer_provider is not None:
        telemetry.instrument(app, upstream_client)
    return app


def run(settings: Settings) -> None:
    uvicorn.run(build_app(settings), host=settings.host, port=settings.port)
