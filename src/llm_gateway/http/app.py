"""FastAPI application factory. Knows routes and error mapping, not how parts are built."""

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp

from llm_gateway.chat.use_case import ChatCompletions
from llm_gateway.http.admission import Admission, LoopLag, ShedWhileLagging
from llm_gateway.http.auth import bearer_tenant
from llm_gateway.http.body_limit import BodyLimit
from llm_gateway.http.errors import register_error_handlers
from llm_gateway.http.routes import CHAT_PATH, chat_router
from llm_gateway.metrics import GatewayMetrics, HttpMetrics, metrics_endpoint
from llm_gateway.routing.breaker import BreakerState, CircuitBreaker
from llm_gateway.tenancy.auth import Authenticator

MCP_PATH = "/mcp"

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_app(
    chat: ChatCompletions,
    authenticator: Authenticator,
    breakers: Sequence[CircuitBreaker] = (),
    lifespan: Lifespan | None = None,
    mcp_app: ASGIApp | None = None,
    metrics: GatewayMetrics | None = None,
    max_request_bytes: int = 4 * 1024 * 1024,
    chat_admission: Admission | None = None,
    tool_admission: Admission | None = None,
) -> FastAPI:
    app = FastAPI(title="LLM Gateway", lifespan=lifespan)
    register_error_handlers(app)
    app.add_middleware(BodyLimit, max_bytes=max_request_bytes)
    admission = chat_admission or Admission(LoopLag())  # a lag that's never measured: 0
    door = {CHAT_PATH: admission}
    if mcp_app is not None and tool_admission is not None:
        door[MCP_PATH] = tool_admission
    # Outside the body limit: a refusal at the door reads nothing.
    app.add_middleware(ShedWhileLagging, admissions=door)
    if metrics is not None:
        app.add_middleware(HttpMetrics, metrics=metrics)
        app.router.routes.append(Route("/metrics", endpoint=metrics_endpoint(metrics)))
    app.include_router(chat_router(chat, bearer_tenant(authenticator), admission))
    if mcp_app is not None:
        # A route, not a mount: clients use the exact URL /mcp, with no trailing slash.
        # Starlette serves a non-function endpoint as a raw ASGI app. POST only: in stateless
        # JSON mode GET (a server-to-client SSE stream that never closes) and DELETE (end a
        # session) have nothing to serve, and an open GET stream would hold a task forever.
        app.router.routes.append(Route(MCP_PATH, endpoint=mcp_app, methods=["POST"]))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        # Not ready only when no provider could take a request: every circuit is open.
        states = {breaker.name: breaker.state for breaker in breakers}
        ready = not states or any(state is not BreakerState.OPEN for state in states.values())
        return JSONResponse(
            {"status": "ready" if ready else "unavailable", "providers": states},
            status_code=200 if ready else 503,
        )

    return app
