"""Adapter for the Anthropic Messages API, through the official SDK.

Translates the gateway's OpenAI-shaped request into a Messages request, and the
Messages stream events back into domain chunks. Nothing Anthropic-shaped leaves here.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import anthropic
from opentelemetry.trace import StatusCode

from llm_gateway.chat.model import ChatRequest, CompletionChunk, Message, Usage
from llm_gateway.chat.ports import ChunkStream, ProviderRejectedError, ProviderUnavailableError
from llm_gateway.providers.openai_compatible import UNAVAILABLE_STATUSES
from llm_gateway.providers.tracing import record_usage, start_chat_span

logger = logging.getLogger(__name__)

# Models that take server-side refusal fallbacks in their "default" form.
REFUSAL_FALLBACK_MODELS = frozenset(
    {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
)
_REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Messages stop reasons, in OpenAI's finish_reason vocabulary.
_FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "max_tokens": "length",
    "model_context_window_exceeded": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}
_SYSTEM_ROLES = frozenset({"system", "developer"})
_CHAT_ROLES = frozenset({"user", "assistant"})


class AnthropicProvider:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        default_max_tokens: int,
        refusal_fallbacks: bool = True,
    ) -> None:
        self._client = client
        self._default_max_tokens = default_max_tokens
        self._refusal_fallbacks = refusal_fallbacks

    async def stream(self, request: ChatRequest) -> ChunkStream:
        span = start_chat_span("anthropic", request)
        try:
            async for chunk in self._stream_upstream(request):
                record_usage(span, chunk.usage)
                yield chunk
        except ProviderUnavailableError as error:
            span.set_status(StatusCode.ERROR, str(error))
            raise
        finally:
            span.end()

    async def _stream_upstream(self, request: ChatRequest) -> ChunkStream:
        try:
            events = await self._open(to_messages_request(request, self._default_max_tokens))
            async with events:
                async for chunk in chunks_from(events):
                    yield chunk
        except anthropic.APIStatusError as error:
            raise _translated(error) from error
        except anthropic.APIConnectionError as error:  # includes timeouts
            raise ProviderUnavailableError(f"anthropic unreachable: {error}") from error
        except (anthropic.APIError, ValueError, AttributeError, KeyError) as error:
            # Malformed stream data: the provider is broken, not the request.
            raise ProviderUnavailableError(f"anthropic sent malformed data: {error}") from error

    async def _open(self, body: dict[str, Any]) -> Any:
        if self._refusal_fallbacks and body["model"] in REFUSAL_FALLBACK_MODELS:
            # On a policy decline, Anthropic re-runs the request on a fallback model itself.
            return await self._client.beta.messages.create(
                **body, stream=True, betas=[_REFUSAL_FALLBACK_BETA], fallbacks="default"
            )
        return await self._client.messages.create(**body, stream=True)


def to_messages_request(request: ChatRequest, default_max_tokens: int) -> dict[str, Any]:
    """OpenAI chat request → Messages request. `temperature` is dropped: the Messages API
    version the SDK speaks (2026-10-21) rejects sampling parameters on every model."""
    system = [_text_of(message) for message in request.messages if message["role"] in _SYSTEM_ROLES]
    body: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_tokens or default_max_tokens,
        "messages": [_to_message(m) for m in request.messages if m["role"] not in _SYSTEM_ROLES],
    }
    if system:
        body["system"] = "\n\n".join(system)
    return body


async def chunks_from(events: Any) -> ChunkStream:
    """Messages stream events → domain chunks: text deltas, the finish reason, then usage."""
    prompt_tokens = 0
    wrote_text = False
    async for event in events:
        if event.type == "message_start":
            prompt_tokens = _prompt_tokens(event.message.usage)
        elif event.type == "content_block_start" and event.content_block.type == "fallback":
            # Anthropic declined and handed over to another model mid-message. What was
            # already streamed can't be taken back; keep the two answers apart.
            logger.warning("anthropic refusal fallback mid-stream")
            if wrote_text:
                yield CompletionChunk(content="\n\n")
        elif event.type == "content_block_delta" and event.delta.type == "text_delta":
            wrote_text = True
            yield CompletionChunk(content=event.delta.text)
        elif event.type == "message_delta":
            # Prompt counts in message_delta are cumulative (they grow when Anthropic
            # re-runs the request on a fallback model); prefer them when present.
            prompt_tokens = _prompt_tokens(event.usage) or prompt_tokens
            stop_reason = event.delta.stop_reason
            yield CompletionChunk(finish_reason=_FINISH_REASONS.get(stop_reason or "", "stop"))
            # Output tokens include thinking, which the tenant pays for too.
            completion_tokens = max(0, int(event.usage.output_tokens or 0))
            yield CompletionChunk(usage=Usage(prompt_tokens, completion_tokens))
            return
    raise ProviderUnavailableError("anthropic stream ended before the message was finished")


def _to_message(message: Message) -> dict[str, Any]:
    role = message["role"]
    if role not in _CHAT_ROLES or message.get("tool_calls"):
        raise ProviderRejectedError(
            400, f"anthropic adapter does not support {role!r} messages yet"
        )
    content = message.get("content")
    if isinstance(content, str):
        return {"role": role, "content": content}
    return {"role": role, "content": [_to_block(part) for part in _parts(content)]}


def _to_block(part: Mapping[str, Any]) -> dict[str, Any]:
    if part.get("type") == "text":
        return {"type": "text", "text": str(part.get("text", ""))}
    if part.get("type") == "image_url":
        return _image_block(str(part.get("image_url", {}).get("url", "")))
    raise ProviderRejectedError(400, f"unsupported content part {part.get('type')!r}")


def _image_block(url: str) -> dict[str, Any]:
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        media_type = header.removeprefix("data:").removesuffix(";base64")
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": data},
        }
    return {"type": "image", "source": {"type": "url", "url": url}}


def _parts(content: object) -> Iterable[Mapping[str, Any]]:
    if not isinstance(content, Sequence):
        raise ProviderRejectedError(400, "message content must be a string or a list of parts")
    return [part for part in content if isinstance(part, Mapping)]


def _text_of(message: Message) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    return "\n".join(str(part.get("text", "")) for part in _parts(content))


def _prompt_tokens(usage: Any) -> int:
    # Cached prompt tokens are reported separately; the prompt is all three.
    return max(
        0,
        int(
            (getattr(usage, "input_tokens", 0) or 0)
            + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        ),
    )


def _translated(error: anthropic.APIStatusError) -> Exception:
    message = _error_message(error)
    status = error.status_code
    # An `error` event mid-stream arrives on a 200 response (e.g. overloaded): try elsewhere.
    if status < 400 or status >= 500 or status in UNAVAILABLE_STATUSES:
        return ProviderUnavailableError(f"anthropic failed ({status}): {message}")
    return ProviderRejectedError(status, message)


def _error_message(error: anthropic.APIStatusError) -> str:
    body = error.body
    if isinstance(body, Mapping) and isinstance(body.get("error"), Mapping):
        return str(body["error"].get("message", error.message))
    return error.message
