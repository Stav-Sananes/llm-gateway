"""Adapter for any upstream speaking the OpenAI chat completions protocol (Ollama, vLLM)."""

import json
from collections.abc import Mapping
from typing import Any

import httpx
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, StatusCode

from llm_gateway.chat.model import ChatRequest, CompletionChunk, Usage
from llm_gateway.chat.ports import ChunkStream, ProviderRejectedError, ProviderUnavailableError
from llm_gateway.providers.sse import DONE_SENTINEL, sse_data

_tracer = trace.get_tracer(__name__)


class OpenAICompatibleProvider:
    def __init__(self, client: httpx.AsyncClient, provider_name: str) -> None:
        self._client = client
        self._provider_name = provider_name

    async def stream(self, request: ChatRequest) -> ChunkStream:
        # Span is started detached (not made current): this generator is resumed
        # from different tasks, and attaching context across yields corrupts it.
        span = self._start_span(request)
        try:
            async for chunk in self._stream_upstream(request):
                _record_usage(span, chunk.usage)
                yield chunk
        except ProviderUnavailableError as error:
            span.set_status(StatusCode.ERROR, str(error))
            raise
        finally:
            span.end()

    async def _stream_upstream(self, request: ChatRequest) -> ChunkStream:
        try:
            async with self._client.stream(
                "POST", "/chat/completions", json=to_wire_request(request)
            ) as response:
                await self._raise_for_status(response)
                async for line in response.aiter_lines():
                    data = sse_data(line)
                    if data == DONE_SENTINEL:
                        return
                    if data:
                        yield parse_chunk(json.loads(data))
        except httpx.TimeoutException as error:
            raise ProviderUnavailableError(f"{self._provider_name} timed out") from error
        except httpx.TransportError as error:
            raise ProviderUnavailableError(f"{self._provider_name} unreachable: {error}") from error
        except json.JSONDecodeError as error:
            raise ProviderUnavailableError(f"{self._provider_name} sent malformed data") from error

    async def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        await response.aread()
        message = _error_message(response)
        if response.status_code >= 500:
            raise ProviderUnavailableError(f"{self._provider_name} failed: {message}")
        raise ProviderRejectedError(response.status_code, message)

    def _start_span(self, request: ChatRequest) -> Span:
        return _tracer.start_span(
            f"chat {request.model}",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": self._provider_name,
                "gen_ai.request.model": request.model,
            },
        )


def to_wire_request(request: ChatRequest) -> dict[str, Any]:
    # The gateway always streams upstream and always asks for usage: metering
    # and non-streaming clients are both served from the same chunk stream.
    body: dict[str, Any] = {
        "model": request.model,
        "messages": list(request.messages),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if request.max_tokens is not None:
        body["max_tokens"] = request.max_tokens
    if request.temperature is not None:
        body["temperature"] = request.temperature
    return body


def parse_chunk(payload: Mapping[str, Any]) -> CompletionChunk:
    choices = payload.get("choices") or []
    first_choice: Mapping[str, Any] = choices[0] if choices else {}
    delta: Mapping[str, Any] = first_choice.get("delta") or {}
    return CompletionChunk(
        content=delta.get("content") or "",
        finish_reason=first_choice.get("finish_reason"),
        usage=_parse_usage(payload.get("usage")),
    )


def _parse_usage(raw: Mapping[str, Any] | None) -> Usage | None:
    if not raw:
        return None
    return Usage(
        prompt_tokens=int(raw.get("prompt_tokens", 0)),
        completion_tokens=int(raw.get("completion_tokens", 0)),
    )


def _error_message(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text or response.reason_phrase
    error = body.get("error") if isinstance(body, Mapping) else None
    if isinstance(error, Mapping):
        return str(error.get("message", error))
    return str(error or response.reason_phrase)


def _record_usage(span: Span, usage: Usage | None) -> None:
    if usage is None:
        return
    span.set_attribute("gen_ai.usage.input_tokens", usage.prompt_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", usage.completion_tokens)
