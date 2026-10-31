"""Inbound OpenAI request body, validated at the edge and converted to the domain model."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from llm_gateway.chat.model import ChatRequest


class StreamOptions(BaseModel):
    include_usage: bool = False


class ChatCompletionBody(BaseModel):
    # Unknown OpenAI fields are ignored rather than rejected (Postel: accept a superset).
    model_config = ConfigDict(extra="ignore")

    model: str = Field(min_length=1)
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False
    stream_options: StreamOptions | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0, le=2)

    @property
    def include_usage(self) -> bool:
        return self.stream_options is not None and self.stream_options.include_usage

    def to_domain(self) -> ChatRequest:
        return ChatRequest(
            model=self.model,
            messages=tuple(self.messages),
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
