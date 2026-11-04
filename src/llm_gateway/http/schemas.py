"""Inbound OpenAI request body, validated at the edge and converted to the domain model."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from llm_gateway.chat.model import ChatRequest


class StreamOptions(BaseModel):
    include_usage: bool = False


# Model names travel into logs, usage rows and metrics: short, printable, valid UTF-8.
MAX_MODEL_NAME_CHARS = 256
# Far above any model's output limit; bounds what a reservation (and a usage row) can hold.
MAX_COMPLETION_TOKENS = 1_000_000


class ChatCompletionBody(BaseModel):
    # Unknown OpenAI fields are ignored rather than rejected (Postel: accept a superset).
    model_config = ConfigDict(extra="ignore")

    model: str = Field(min_length=1, max_length=MAX_MODEL_NAME_CHARS)
    messages: list[dict[str, Any]] = Field(min_length=1)
    stream: bool = False
    stream_options: StreamOptions | None = None
    max_tokens: int | None = Field(default=None, gt=0, le=MAX_COMPLETION_TOKENS)
    temperature: float | None = Field(default=None, ge=0, le=2)
    # Not forwarded yet; only noted, so such requests never use the semantic cache.
    tools: list[Any] | None = None
    functions: list[Any] | None = None

    @field_validator("model")
    @classmethod
    def _printable_model_name(cls, model: str) -> str:
        try:
            model.encode("utf-8")  # a lone surrogate can't be stored or logged
        except UnicodeEncodeError:
            raise ValueError("model must be valid UTF-8") from None
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in model):
            raise ValueError("model must not contain control characters")
        return model

    @property
    def include_usage(self) -> bool:
        return self.stream_options is not None and self.stream_options.include_usage

    def to_domain(self) -> ChatRequest:
        return ChatRequest(
            model=self.model,
            messages=tuple(self.messages),
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            has_tools=bool(self.tools or self.functions),
        )
