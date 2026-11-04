import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from llm_gateway.chat.model import ChatRequest, CompletionChunk, Origin, Usage, collect
from llm_gateway.chat.ports import ProviderRejectedError, ProviderUnavailableError
from llm_gateway.chat.resume import CONTINUE_INSTRUCTION
from llm_gateway.routing.router import Router, Target
from tests.support.providers import ScriptedProvider

pytestmark = pytest.mark.anyio

FROM_A = (CompletionChunk(content="from a"),)
FROM_B = (CompletionChunk(content="from b"),)
SMART = [Target("a", "a-large"), Target("b", "b-small")]


def a_request(model: str) -> ChatRequest:
    return ChatRequest(model=model, messages=({"role": "user", "content": "hi"},))


def router(
    a: ScriptedProvider, b: ScriptedProvider, spans: InMemorySpanExporter | None = None
) -> Router:
    tracing = TracerProvider()
    if spans is not None:
        tracing.add_span_processor(SimpleSpanProcessor(spans))
    return Router(
        {"a": a, "b": b}, {"smart": SMART}, default_provider="a", tracer=tracing.get_tracer("t")
    )


def text(*parts: str) -> tuple[CompletionChunk, ...]:
    return tuple(CompletionChunk(content=part) for part in parts)


FINISHED = (CompletionChunk(finish_reason="stop"), CompletionChunk(usage=Usage(30, 4)))


async def test_an_unrouted_model_goes_to_the_default_provider_unchanged() -> None:
    a, b = ScriptedProvider(FROM_A), ScriptedProvider(FROM_B)

    completion = await collect(router(a, b).stream(a_request("llama3")))

    assert completion.content == "from a"
    assert [r.model for r in a.requests] == ["llama3"]


async def test_a_routed_model_goes_to_its_first_target_with_that_targets_model() -> None:
    a, b = ScriptedProvider(FROM_A), ScriptedProvider(FROM_B)

    await collect(router(a, b).stream(a_request("smart")))

    assert [r.model for r in a.requests] == ["a-large"]
    assert b.requests == []


async def test_falls_back_when_a_target_is_unavailable_before_its_first_chunk() -> None:
    a, b = ScriptedProvider((), fail_after=True), ScriptedProvider(FROM_B)

    completion = await collect(router(a, b).stream(a_request("smart")))

    assert completion.content == "from b"
    assert [r.model for r in b.requests] == ["b-small"]
    assert a.closed


async def test_falls_back_when_a_target_answers_with_an_empty_stream() -> None:
    a, b = ScriptedProvider(()), ScriptedProvider(FROM_B)

    completion = await collect(router(a, b).stream(a_request("smart")))

    assert completion.content == "from b"


async def test_does_not_fall_back_on_a_rejected_request() -> None:
    a, b = ScriptedProvider(FROM_A, rejects=True), ScriptedProvider(FROM_B)

    with pytest.raises(ProviderRejectedError):
        await collect(router(a, b).stream(a_request("smart")))

    assert b.requests == []


async def test_every_target_unavailable_is_unavailable_naming_them_all() -> None:
    a, b = ScriptedProvider((), fail_after=True), ScriptedProvider((), fail_after=True)

    with pytest.raises(ProviderUnavailableError, match=r"a: .*; b: "):
        await collect(router(a, b).stream(a_request("smart")))


async def test_closing_the_stream_early_releases_the_serving_provider() -> None:
    a = ScriptedProvider(FROM_A + FROM_A)
    stream = router(a, ScriptedProvider(FROM_B)).stream(a_request("smart"))

    await anext(stream)
    await stream.aclose()

    assert a.closed


def test_routes_naming_an_unknown_provider_are_refused_at_startup() -> None:
    with pytest.raises(ValueError, match="nope"):
        Router({"a": ScriptedProvider(FROM_A)}, {"x": [Target("nope", "m")]}, "a")


async def test_the_first_chunk_a_target_serves_names_it() -> None:
    a = ScriptedProvider((), fail_after=True)
    b = ScriptedProvider((CompletionChunk(content="x"), CompletionChunk(content="y")))

    chunks = [chunk async for chunk in router(a, b).stream(a_request("smart"))]

    assert [chunk.origin for chunk in chunks] == [Origin("b", "b-small"), None]


# Resuming mid-answer


async def test_a_target_that_dies_mid_answer_is_continued_by_the_next() -> None:
    a = ScriptedProvider(text("The capital", " of France"), fail_after=True)
    b = ScriptedProvider(text(" is Paris.") + FINISHED)

    completion = await collect(router(a, b).stream(a_request("smart")))

    assert completion.content == "The capital of France is Paris."
    assert completion.finish_reason == "stop"
    (asked,) = b.requests
    assert asked.model == "b-small"
    assert asked.messages[-2:] == (
        {"role": "assistant", "content": "The capital of France"},
        {"role": "user", "content": CONTINUE_INSTRUCTION},
    )


async def test_a_continuation_repeating_the_answers_end_is_joined_without_the_repeat() -> None:
    a = ScriptedProvider(text("The capital of France"), fail_after=True)
    b = ScriptedProvider(text("of France is", " Paris.") + FINISHED)

    completion = await collect(router(a, b).stream(a_request("smart")))

    assert completion.content == "The capital of France is Paris."


async def test_the_continuing_targets_first_chunk_names_it() -> None:
    a = ScriptedProvider(text("The capital", " of France"), fail_after=True)
    b = ScriptedProvider(text(" is Paris."))

    chunks = [chunk async for chunk in router(a, b).stream(a_request("smart"))]

    assert [chunk.origin for chunk in chunks] == [
        Origin("a", "a-large"),
        None,
        Origin("b", "b-small"),
    ]


async def test_with_no_target_left_to_continue_the_answer_breaks() -> None:
    a = ScriptedProvider(text("The capital"), fail_after=True)
    b = ScriptedProvider((), fail_after=True)

    with pytest.raises(ProviderUnavailableError, match=r"a: died mid-answer.*; b: "):
        await collect(router(a, b).stream(a_request("smart")))

    assert len(b.requests) == 1  # it was asked to continue


async def test_an_answer_that_finished_before_its_stream_broke_is_not_continued() -> None:
    a = ScriptedProvider((*text("Paris."), CompletionChunk(finish_reason="stop")), fail_after=True)
    b = ScriptedProvider(text("extra"))

    with pytest.raises(ProviderUnavailableError):
        await collect(router(a, b).stream(a_request("smart")))

    assert b.requests == []


async def test_the_continuation_may_only_use_what_is_left_of_max_tokens() -> None:
    a = ScriptedProvider(text("x" * 30), fail_after=True)
    b = ScriptedProvider(text("y"))
    capped = ChatRequest("smart", ({"role": "user", "content": "hi"},), max_tokens=100)

    await collect(router(a, b).stream(capped))

    assert b.requests[0].max_tokens == 90


async def test_an_answer_that_used_all_its_max_tokens_ends_without_asking_again() -> None:
    a = ScriptedProvider(text("x" * 30), fail_after=True)
    b = ScriptedProvider(text("y"))
    capped = ChatRequest("smart", ({"role": "user", "content": "hi"},), max_tokens=10)

    completion = await collect(router(a, b).stream(capped))

    assert (completion.content, completion.finish_reason) == ("x" * 30, "length")
    assert b.requests == []


async def test_a_resume_is_traced_with_both_targets() -> None:
    spans = InMemorySpanExporter()
    a = ScriptedProvider(text("The capital"), fail_after=True)
    b = ScriptedProvider(text(" of France."))

    await collect(router(a, b, spans).stream(a_request("smart")))

    (span,) = spans.get_finished_spans()
    assert span.name == "gateway.resume"
    assert dict(span.attributes or {}) == {
        "gen_ai.request.model": "smart",
        "gateway.resume.from.provider": "a",
        "gateway.resume.from.model": "a-large",
        "gateway.resume.to.provider": "b",
        "gateway.resume.to.model": "b-small",
        "gateway.resume.partial_chars": len("The capital"),
    }


async def test_closing_the_stream_during_a_continuation_releases_the_continuing_target() -> None:
    a = ScriptedProvider(text("The capital"), fail_after=True)
    b = ScriptedProvider(text(" of France", " is Paris."))
    stream = router(a, b).stream(a_request("smart"))

    assert (await anext(stream)).content == "The capital"
    assert (await anext(stream)).content == " of France"
    await stream.aclose()

    assert a.closed and b.closed
