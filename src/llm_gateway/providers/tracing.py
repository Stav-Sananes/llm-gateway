"""One client span per provider call, with the GenAI semantic-convention attributes."""

from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind

from llm_gateway.chat.model import ChatRequest, Usage

_tracer = trace.get_tracer(__name__)


def start_chat_span(provider_name: str, request: ChatRequest) -> Span:
    # Started detached (not made current): provider streams are resumed from different
    # tasks, and attaching context across yields corrupts it.
    return _tracer.start_span(
        f"chat {request.model}",
        kind=SpanKind.CLIENT,
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": provider_name,
            "gen_ai.request.model": request.model,
        },
    )


def record_usage(span: Span, usage: Usage | None) -> None:
    if usage is None:
        return
    span.set_attribute("gen_ai.usage.input_tokens", usage.prompt_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", usage.completion_tokens)
