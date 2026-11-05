"""The OpenAI-compatible chat completions endpoint."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

from llm_gateway.cache.model import CacheDirectives
from llm_gateway.cache.semantic import CACHE_PROVIDER
from llm_gateway.chat.model import CompletionChunk, collect
from llm_gateway.chat.ports import ChunkStream
from llm_gateway.chat.use_case import ChatCompletions
from llm_gateway.http.admission import Admission, Ticket
from llm_gateway.http.auth import TenantDependency
from llm_gateway.http.presenter import ResponseIdentity, completion_payload, render_sse
from llm_gateway.http.schemas import ChatCompletionBody
from llm_gateway.tenancy.model import Tenant

_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

CHAT_PATH = "/v1/chat/completions"


def chat_router(
    chat: ChatCompletions, authenticated: TenantDependency, admission: Admission
) -> APIRouter:
    router = APIRouter()

    @router.post(CHAT_PATH, response_model=None)
    async def create_chat_completion(
        body: ChatCompletionBody,
        tenant: Annotated[Tenant, Depends(authenticated)],
        cache_control: Annotated[str | None, Header()] = None,
    ) -> StreamingResponse | JSONResponse:
        # Here, after the body is read and the key checked: nothing before holds a place.
        ticket = admission.admit()  # AtCapacityError → 503
        try:
            identity = ResponseIdentity.new(body.model)
            directives = CacheDirectives.from_header(cache_control)
            chunks = await chat.start(body.to_domain(), tenant, directives)
            # Prime the stream: pull the first chunk before sending headers, so an
            # upstream that fails up front still gets a real HTTP error status.
            first = await anext(chunks, None)
        except BaseException:
            ticket.release()
            raise
        headers = {"X-Gateway-Cache": "hit" if _from_cache(first) else "miss"}
        if not body.stream:
            try:
                completion = await collect(_prepended(first, chunks))
            finally:
                ticket.release()
            return JSONResponse(completion_payload(identity, completion), headers=headers)
        return _ReleasingStream(
            ticket,
            render_sse(identity, first, chunks, include_usage=body.include_usage),
            media_type="text/event-stream",
            headers=_SSE_HEADERS | headers,
        )

    return router


class _ReleasingStream(StreamingResponse):
    """Frees its admission ticket once the response is over, however it ends: finished,
    failed, or the client gone before the first byte."""

    def __init__(self, ticket: Ticket, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ticket = ticket

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._ticket.release()


def _from_cache(chunk: CompletionChunk | None) -> bool:
    return (
        chunk is not None and chunk.origin is not None and chunk.origin.provider == CACHE_PROVIDER
    )


async def _prepended(first: CompletionChunk | None, rest: ChunkStream) -> ChunkStream:
    try:
        if first is not None:
            yield first
        async for chunk in rest:
            yield chunk
    finally:
        await rest.aclose()
