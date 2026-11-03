"""The Anthropic adapter, driving the real SDK against a fake Messages API on a real socket."""

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from pydantic import SecretStr

from harness.fake_anthropic import FakeAnthropicLog, FakeAnthropicScript, create_fake_anthropic
from llm_gateway.chat.model import ChatRequest, Completion, collect
from llm_gateway.chat.ports import ProviderRejectedError, ProviderUnavailableError
from llm_gateway.config import Settings
from llm_gateway.main import build_anthropic_client
from llm_gateway.providers.anthropic import AnthropicProvider
from tests.support.servers import serving, silent_local_url, unused_local_url

pytestmark = pytest.mark.anyio

HI = ({"role": "user", "content": "hi"},)


def a_request(model: str = "claude-haiku-5-5", **fields: object) -> ChatRequest:
    return ChatRequest(model=model, messages=fields.pop("messages", HI), max_tokens=50, **fields)  # type: ignore[arg-type]


@asynccontextmanager
async def provider_at(base_url: str) -> AsyncIterator[AnthropicProvider]:
    settings = Settings(
        anthropic_api_key=SecretStr("sk-test"),
        anthropic_base_url=base_url,
        upstream_read_timeout_s=0.5,
    )
    client = build_anthropic_client(settings)
    assert client is not None
    try:
        yield AnthropicProvider(client, default_max_tokens=4096)
    finally:
        await client.close()  # its connection pool belongs to this test's event loop


@asynccontextmanager
async def fake(
    script: FakeAnthropicScript, log: FakeAnthropicLog | None = None
) -> AsyncIterator[AnthropicProvider]:
    with serving(create_fake_anthropic(script, log)) as url:
        async with provider_at(url) as provider:
            yield provider


async def complete(provider: AnthropicProvider, request: ChatRequest) -> Completion:
    return await collect(provider.stream(request))


async def test_streams_text_finish_reason_and_usage() -> None:
    async with fake(FakeAnthropicScript()) as provider:
        completion = await complete(provider, a_request())

    assert completion.content == "Hello from fake Claude."
    assert completion.finish_reason == "stop"
    assert completion.usage is not None
    assert (completion.usage.prompt_tokens, completion.usage.completion_tokens) == (9, 4)


async def test_sends_system_messages_as_the_system_prompt_and_drops_temperature() -> None:
    log = FakeAnthropicLog()
    messages = (
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "hi"},
    )
    async with fake(FakeAnthropicScript(), log) as provider:
        await complete(provider, a_request(messages=messages, temperature=0.2))

    [sent] = log.requests
    assert sent["system"] == "Be brief."
    assert sent["messages"] == [{"role": "user", "content": "hi"}]
    assert sent["max_tokens"] == 50
    assert "temperature" not in sent
    assert log.headers[0]["authorization"] == "Bearer sk-test"  # how SDK 2.x sends the key


async def test_refusal_fallbacks_are_requested_only_for_models_that_take_them() -> None:
    log = FakeAnthropicLog()
    async with fake(FakeAnthropicScript(), log) as provider:
        await complete(provider, a_request(model="claude-opus-5-5"))
        await complete(provider, a_request(model="claude-haiku-5-5"))

    supported, other = log.requests
    assert supported["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in log.headers[0]["anthropic-beta"]
    assert "fallbacks" not in other
    assert "anthropic-beta" not in log.headers[1]


async def test_refusal_is_a_content_filter_finish() -> None:
    async with fake(FakeAnthropicScript(reply=("I can't",), stop_reason="refusal")) as provider:
        completion = await complete(provider, a_request())

    assert completion.finish_reason == "content_filter"


@pytest.mark.parametrize("status", [401, 429, 500, 529])
async def test_statuses_meaning_try_elsewhere_are_unavailable(status: int) -> None:
    async with fake(FakeAnthropicScript(fail_status=status)) as provider:
        with pytest.raises(ProviderUnavailableError):
            await complete(provider, a_request())


@pytest.mark.parametrize("status", [400, 404])
async def test_bad_requests_keep_their_status_and_message(status: int) -> None:
    script = FakeAnthropicScript(fail_status=status, fail_message="model: unknown")
    async with fake(script) as provider:
        with pytest.raises(ProviderRejectedError) as rejected:
            await complete(provider, a_request())

    assert rejected.value.status_code == status
    assert rejected.value.message == "model: unknown"


async def test_error_event_mid_stream_is_unavailable_after_the_partial_text() -> None:
    seen: list[str] = []
    async with fake(FakeAnthropicScript(error_after_chunks=2)) as provider:
        with pytest.raises(ProviderUnavailableError):
            async for chunk in provider.stream(a_request()):
                seen.append(chunk.content)

    assert "".join(seen) == "Hello from"


async def test_tool_messages_are_rejected_before_calling_anthropic() -> None:
    log = FakeAnthropicLog()
    tool_reply = ({"role": "tool", "content": "42", "tool_call_id": "c1"},)
    async with fake(FakeAnthropicScript(), log) as provider:
        with pytest.raises(ProviderRejectedError):
            await complete(provider, a_request(messages=tool_reply))

    assert log.requests == []


async def test_unreachable_anthropic_is_unavailable() -> None:
    async with provider_at(unused_local_url()) as provider:
        with pytest.raises(ProviderUnavailableError):
            await complete(provider, a_request())


async def test_hung_anthropic_is_given_up_on_after_the_read_timeout() -> None:
    started = time.monotonic()
    with silent_local_url() as url:
        async with provider_at(url) as provider:
            with pytest.raises(ProviderUnavailableError):
                await complete(provider, a_request())

    assert time.monotonic() - started < 2.0  # read timeout 0.5 s, no SDK retries
