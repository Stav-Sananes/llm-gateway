"""The OpenAI-compatible chat completions endpoint."""

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from llm_gateway.chat.model import collect
from llm_gateway.chat.use_case import ChatCompletions
from llm_gateway.http.auth import TenantDependency
from llm_gateway.http.presenter import ResponseIdentity, completion_payload, render_sse
from llm_gateway.http.schemas import ChatCompletionBody

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def chat_router(chat: ChatCompletions, tenant: TenantDependency) -> APIRouter:
    router = APIRouter(dependencies=[Depends(tenant)])

    @router.post("/v1/chat/completions", response_model=None)
    async def create_chat_completion(
        body: ChatCompletionBody,
    ) -> StreamingResponse | dict[str, Any]:
        identity = ResponseIdentity.new(body.model)
        chunks = chat.stream(body.to_domain())
        if not body.stream:
            return completion_payload(identity, await collect(chunks))

        # Prime the stream: pull the first chunk before sending headers, so an
        # upstream that fails up front still gets a real HTTP error status.
        first = await anext(chunks, None)
        return StreamingResponse(
            render_sse(identity, first, chunks, include_usage=body.include_usage),
            media_type="text/event-stream",
            headers=_SSE_HEADERS,
        )

    return router
