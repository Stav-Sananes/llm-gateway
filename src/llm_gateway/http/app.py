"""FastAPI application factory. Knows routes and error mapping, not how parts are built."""

from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from llm_gateway.chat.use_case import ChatCompletions
from llm_gateway.http.auth import bearer_tenant
from llm_gateway.http.errors import register_error_handlers
from llm_gateway.http.routes import chat_router
from llm_gateway.routing.breaker import BreakerState, CircuitBreaker
from llm_gateway.tenancy.auth import Authenticator

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_app(
    chat: ChatCompletions,
    authenticator: Authenticator,
    breakers: Sequence[CircuitBreaker] = (),
    lifespan: Lifespan | None = None,
) -> FastAPI:
    app = FastAPI(title="LLM Gateway", lifespan=lifespan)
    register_error_handlers(app)
    app.include_router(chat_router(chat, bearer_tenant(authenticator)))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        # Not ready only when no provider could take a request: every circuit is open.
        states = {breaker.name: breaker.state for breaker in breakers}
        ready = any(state is not BreakerState.OPEN for state in states.values())
        return JSONResponse(
            {"status": "ready" if ready else "unavailable", "providers": states},
            status_code=200 if ready else 503,
        )

    return app
