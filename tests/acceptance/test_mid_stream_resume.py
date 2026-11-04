"""Mid-stream resume end to end: fake Claude dies mid-answer, the fake LLM continues it."""

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import select

from harness.fake_anthropic import FakeAnthropicScript, create_fake_anthropic
from harness.fake_llm import FakeLlmLog, FakeLlmScript, create_fake_llm
from llm_gateway.audit.switches import SWITCHED
from llm_gateway.chat.resume import CONTINUE_INSTRUCTION
from llm_gateway.config import RouteTarget, Settings
from llm_gateway.main import build_app, build_engine
from llm_gateway.storage.tables import audit_events
from llm_gateway.storage.usage_stream import STREAM, decode
from llm_gateway.usage.model import Outcome, UsageEvent
from tests.support.backing_services import new_api_key
from tests.support.servers import serving

QUESTION: dict[str, Any] = {"model": "smart", "messages": [{"role": "user", "content": "hi"}]}
# Claude says "Hello from" and dies; the fake LLM, asked to continue, starts over:
# "Hello from the fake LLM." The seam drops the repeat.
DIES_MID_ANSWER = FakeAnthropicScript(
    reply=("Hello", " from", " fake", " Claude."), error_after_chunks=2
)


@contextmanager
def gateway(services: Settings, local_log: FakeLlmLog) -> Iterator[httpx.Client]:
    api_key = new_api_key(services)
    with (
        serving(create_fake_anthropic(DIES_MID_ANSWER)) as claude_url,
        serving(create_fake_llm(FakeLlmScript(), local_log)) as local_url,
    ):
        settings = services.model_copy(
            update={
                "upstream_name": "local",
                "upstream_base_url": f"{local_url}/v1",
                "anthropic_api_key": SecretStr("sk-test"),
                "anthropic_base_url": claude_url,
                "routes": {
                    "smart": [
                        RouteTarget(provider="anthropic", model="claude-opus-5-5"),
                        RouteTarget(provider="local", model="qwen2.5:0.5b"),
                    ]
                },
            }
        )
        with (
            serving(build_app(settings)) as url,
            httpx.Client(base_url=url, headers={"Authorization": f"Bearer {api_key}"}) as client,
        ):
            yield client


def streamed(client: httpx.Client) -> list[dict[str, Any]]:
    body = QUESTION | {"stream": True, "stream_options": {"include_usage": True}}
    with client.stream("POST", "/v1/chat/completions", json=body) as response:
        assert response.status_code == 200
        lines = [line for line in response.iter_lines() if line.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    return [json.loads(line.removeprefix("data: ")) for line in lines[:-1]]


def queued_usage(settings: Settings) -> list[UsageEvent]:
    async def read() -> list[UsageEvent]:
        redis = Redis.from_url(settings.redis_url)
        try:
            entries = await redis.xrange(STREAM) or []
            raw = [fields[b"event"] for _, fields in entries if fields]
            return [decode(r.decode() if isinstance(r, bytes) else r) for r in raw]
        finally:
            await redis.aclose()

    return asyncio.run(read())


def audited_actions(settings: Settings) -> list[dict[str, Any]]:
    async def read() -> list[dict[str, Any]]:
        engine = build_engine(settings)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(select(audit_events.c.payload))
                return [json.loads(row.payload) for row in rows]
        finally:
            await engine.dispose()

    return asyncio.run(read())


def test_the_client_sees_one_unbroken_answer(services: Settings) -> None:
    local_log = FakeLlmLog()
    with gateway(services, local_log) as client:
        chunks = streamed(client)

    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks if c["choices"])
    assert text == "Hello from the fake LLM."
    assert {c["model"] for c in chunks} == {"smart"}  # nothing on the wire names a provider
    assert [c["choices"][0]["finish_reason"] for c in chunks if c["choices"]][-1] == "stop"
    (continued,) = local_log.requests
    assert continued["messages"][-2:] == [
        {"role": "assistant", "content": "Hello from"},
        {"role": "user", "content": CONTINUE_INSTRUCTION},
    ]


def test_the_switch_is_recorded_as_usage_per_provider_and_on_the_audit_chain(
    services: Settings,
) -> None:
    with gateway(services, FakeLlmLog()) as client:
        streamed(client)

    events = queued_usage(services)
    assert [(e.provider, e.outcome) for e in events] == [
        ("anthropic", Outcome.RESUMED),
        ("local", Outcome.COMPLETED),
    ]
    (switch,) = [a for a in audited_actions(services) if a["action"] == SWITCHED]
    assert switch["details"]["from"] == {"provider": "anthropic", "model": "claude-opus-5-5"}
    assert switch["details"]["to"] == {"provider": "local", "model": "qwen2.5:0.5b"}
