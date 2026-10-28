"""Domain vocabulary for chat completions.

The gateway's public contract is OpenAI-compatible, so messages keep the OpenAI
shape. Everything a provider streams back is normalized into CompletionChunk,
so no provider wire format leaks past its adapter.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass

Message = Mapping[str, object]


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: Sequence[Message]
    max_tokens: int | None = None
    temperature: float | None = None


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class CompletionChunk:
    content: str = ""
    finish_reason: str | None = None
    usage: Usage | None = None


@dataclass(frozen=True)
class Completion:
    content: str
    finish_reason: str | None
    usage: Usage | None


async def collect(chunks: AsyncIterator[CompletionChunk]) -> Completion:
    """Fold a chunk stream into one completion (used for non-streaming clients)."""
    parts: list[str] = []
    finish_reason: str | None = None
    usage: Usage | None = None
    async for chunk in chunks:
        parts.append(chunk.content)
        finish_reason = chunk.finish_reason or finish_reason
        usage = chunk.usage or usage
    return Completion(content="".join(parts), finish_reason=finish_reason, usage=usage)
