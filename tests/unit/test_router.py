import pytest

from llm_gateway.chat.model import ChatRequest, CompletionChunk, collect
from llm_gateway.chat.ports import ProviderRejectedError, ProviderUnavailableError
from llm_gateway.routing.router import Router, Target
from tests.support.providers import ScriptedProvider

pytestmark = pytest.mark.anyio

FROM_A = (CompletionChunk(content="from a"),)
FROM_B = (CompletionChunk(content="from b"),)
SMART = [Target("a", "a-large"), Target("b", "b-small")]


def a_request(model: str) -> ChatRequest:
    return ChatRequest(model=model, messages=({"role": "user", "content": "hi"},))


def router(a: ScriptedProvider, b: ScriptedProvider) -> Router:
    return Router({"a": a, "b": b}, {"smart": SMART}, default_provider="a")


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


async def test_does_not_fall_back_once_a_target_has_streamed() -> None:
    a, b = ScriptedProvider(FROM_A, fail_after=True), ScriptedProvider(FROM_B)

    with pytest.raises(ProviderUnavailableError):
        await collect(router(a, b).stream(a_request("smart")))

    assert b.requests == []


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
