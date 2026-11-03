"""The OpenAI-compatible chat completions endpoint."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from llm_gateway.chat.model import collect
from llm_gateway.chat.use_case import ChatCompletions
from llm_gateway.http.auth import TenantDependency
from llm_gateway.http.presenter import ResponseIdentity, completion_payload, render_sse
from llm_gateway.http.schemas import ChatCompletionBody
from llm_gateway.tenancy.model import Tenant

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def chat_router(chat: ChatCompletions, authenticated: TenantDependency) -> APIRouter:
    router = APIRouter()

    @router.post("/v1/chat/completions", response_model=None)
    async def create_chat_completion(
        body: ChatCompletionBody, tenant: Annotated[Tenant, Depends(authenticated)]
    ) -> StreamingResponse | dict[str, Any]:
        identity = ResponseIdentity.new(body.model)
        chunks = await chat.start(body.to_domain(), tenant)
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
