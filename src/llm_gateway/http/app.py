"""FastAPI application factory. Knows routes and error mapping, not how parts are built."""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from fastapi import FastAPI

from llm_gateway.chat.use_case import ChatCompletions
from llm_gateway.http.errors import register_error_handlers
from llm_gateway.http.routes import chat_router

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]


def create_app(chat: ChatCompletions, lifespan: Lifespan | None = None) -> FastAPI:
    app = FastAPI(title="LLM Gateway", lifespan=lifespan)
    register_error_handlers(app)
    app.include_router(chat_router(chat))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
