"""Routing, fallback and circuit breakers end to end: fake Anthropic in front of a fake LLM."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
from pydantic import SecretStr

from harness.fake_anthropic import FakeAnthropicLog, FakeAnthropicScript, create_fake_anthropic
from harness.fake_llm import FakeLlmLog, FakeLlmScript, create_fake_llm
from llm_gateway.config import RouteTarget, Settings
from llm_gateway.main import build_app
from tests.support.backing_services import new_api_key
from tests.support.servers import serving

SMART: dict[str, Any] = {"model": "smart", "messages": [{"role": "user", "content": "hi"}]}
CLAUDE_TEXT, FALLBACK_TEXT = "Hello from fake Claude.", "Hello from the fake LLM."


@contextmanager
def gateway(
    services: Settings,
    claude: FakeAnthropicScript,
    claude_log: FakeAnthropicLog | None = None,
    fallback_log: FakeLlmLog | None = None,
    fallback: FakeLlmScript = FakeLlmScript(),  # noqa: B008 - frozen, never mutated
) -> Iterator[httpx.Client]:
    api_key = new_api_key(services)
    with (
        serving(create_fake_anthropic(claude, claude_log)) as claude_url,
        serving(create_fake_llm(fallback, fallback_log)) as fallback_url,
    ):
        settings = services.model_copy(
            update={
                "upstream_name": "local",
                "upstream_base_url": f"{fallback_url}/v1",
                "anthropic_api_key": SecretStr("sk-test"),
                "anthropic_base_url": claude_url,
                "breaker_failure_threshold": 2,
                "routes": {
                    "smart": [
                        RouteTarget(provider="anthropic", model="claude-opus-5-5"),
                        RouteTarget(provider="local", model="qwen2.5:0.5b"),
                    ]
                },
            }
        )
        headers = {"Authorization": f"Bearer {api_key}"}
        with (
            serving(build_app(settings)) as url,
            httpx.Client(base_url=url, headers=headers) as client,
        ):
            yield client


def answer(client: httpx.Client, body: dict[str, Any] = SMART) -> httpx.Response:
    return client.post("/v1/chat/completions", json=body)


def test_a_routed_model_is_served_by_its_first_target(services: Settings) -> None:
    claude_log = FakeAnthropicLog()
    with gateway(services, FakeAnthropicScript(), claude_log) as client:
        response = answer(client)

    assert response.json()["choices"][0]["message"]["content"] == CLAUDE_TEXT
    assert [r["model"] for r in claude_log.requests] == ["claude-opus-5-5"]


def test_streaming_through_anthropic_ends_with_usage_and_done(services: Settings) -> None:
    body = SMART | {"stream": True, "stream_options": {"include_usage": True}}
    with gateway(services, FakeAnthropicScript()) as client:
        lines = [line for line in answer(client, body).iter_lines() if line.startswith("data: ")]

    assert lines[-1] == "data: [DONE]"
    assert '"total_tokens":13' in lines[-2]  # 9 prompt + 4 completion


def test_anthropic_overloaded_falls_back_to_the_next_target(services: Settings) -> None:
    fallback_log = FakeLlmLog()
    with gateway(services, FakeAnthropicScript(fail_status=529), fallback_log=fallback_log) as c:
        response = answer(c)

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == FALLBACK_TEXT
    assert [r["model"] for r in fallback_log.requests] == ["qwen2.5:0.5b"]


def test_after_repeated_failures_the_circuit_opens_and_anthropic_is_skipped(
    services: Settings,
) -> None:
    claude_log = FakeAnthropicLog()
    with gateway(services, FakeAnthropicScript(fail_status=500), claude_log) as client:
        statuses = [answer(client).status_code for _ in range(4)]
        ready = client.get("/readyz")

    assert statuses == [200, 200, 200, 200]  # all served by the fallback
    assert len(claude_log.requests) == 2  # the threshold; then the open circuit skips it
    assert ready.status_code == 200
    assert ready.json()["providers"] == {"local": "closed", "anthropic": "open"}


def test_a_bad_request_is_not_retried_elsewhere(services: Settings) -> None:
    fallback_log = FakeLlmLog()
    script = FakeAnthropicScript(fail_status=400, fail_message="messages: roles must alternate")
    with gateway(services, script, fallback_log=fallback_log) as client:
        response = answer(client)

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "messages: roles must alternate"
    assert fallback_log.requests == []


def test_an_unrouted_model_goes_to_the_default_provider(services: Settings) -> None:
    claude_log = FakeAnthropicLog()
    with gateway(services, FakeAnthropicScript(), claude_log) as client:
        response = answer(client, SMART | {"model": "llama3"})

    assert response.json()["choices"][0]["message"]["content"] == FALLBACK_TEXT
    assert claude_log.requests == []


def test_readyz_reports_every_provider(services: Settings) -> None:
    with gateway(services, FakeAnthropicScript()) as client:
        ready = client.get("/readyz")

    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "providers": {"local": "closed", "anthropic": "closed"},
    }


def test_every_provider_failing_is_a_502_and_not_ready_once_all_circuits_open(
    services: Settings,
) -> None:
    claude, local = FakeAnthropicScript(fail_status=500), FakeLlmScript(fail_status=500)
    with gateway(services, claude, fallback=local) as client:
        statuses = [answer(client).status_code for _ in range(2)]
        ready = client.get("/readyz")

    assert statuses == [502, 502]
    assert ready.status_code == 503
    assert ready.json()["status"] == "unavailable"
