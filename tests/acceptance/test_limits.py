"""Per-tenant rate limits and token budgets, end to end with real Postgres and Redis."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import httpx

from harness.fake_llm import FakeLlmLog, FakeLlmScript, create_fake_llm
from llm_gateway.config import Settings
from llm_gateway.main import build_app, build_redis
from tests.support.backing_services import new_api_key
from tests.support.servers import serving, unused_local_url

HELLO: dict[str, Any] = {"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]}
# FakeLlmScript() reports prompt_tokens=7 plus one completion token per reply part (5).
FAKE_TOTAL_TOKENS = 12


@contextmanager
def gateway(
    settings: Settings, api_key: str, log: FakeLlmLog | None = None
) -> Iterator[httpx.Client]:
    with serving(create_fake_llm(FakeLlmScript(), log)) as upstream_url:
        settings = settings.model_copy(update={"upstream_base_url": f"{upstream_url}/v1"})
        headers = {"Authorization": f"Bearer {api_key}"}
        with (
            serving(build_app(settings)) as url,
            httpx.Client(base_url=url, headers=headers) as client,
        ):
            yield client


def tokens_used_on(settings: Settings, day: str) -> int:
    async def read() -> int:
        redis = build_redis(settings)
        try:
            [key] = await redis.keys(f"gw:budget:*:{day}")
            used = await redis.get(key)
            assert used is not None
            return int(used)
        finally:
            await redis.aclose()

    return asyncio.run(read())


def test_requests_beyond_the_burst_get_a_429_with_retry_after(services: Settings) -> None:
    settings = services.model_copy(update={"rate_limit_burst": 2, "rate_limit_per_s": 0.1})
    with gateway(settings, new_api_key(services)) as client:
        statuses = [client.post("/v1/chat/completions", json=HELLO) for _ in range(3)]

    assert [r.status_code for r in statuses] == [200, 200, 429]
    assert statuses[2].headers["retry-after"] == "10"
    assert statuses[2].json()["error"]["code"] == "rate_limit_exceeded"


def test_each_tenant_has_its_own_rate_limit(services: Settings) -> None:
    settings = services.model_copy(update={"rate_limit_burst": 1, "rate_limit_per_s": 0.1})
    acme, globex = new_api_key(services, "acme"), new_api_key(services, "globex")
    with gateway(settings, acme) as client:
        client.post("/v1/chat/completions", json=HELLO)
        other = client.post(
            "/v1/chat/completions", json=HELLO, headers={"Authorization": f"Bearer {globex}"}
        )

    assert other.status_code == 200


def test_a_finished_request_is_charged_its_actual_usage(services: Settings) -> None:
    today = datetime.now(UTC).date().isoformat()  # before the request, in case it spans midnight
    with gateway(services, new_api_key(services)) as client:
        client.post("/v1/chat/completions", json=HELLO | {"max_tokens": 500, "stream": True})

    assert tokens_used_on(services, today) == FAKE_TOTAL_TOKENS


def test_a_request_without_max_tokens_is_capped_upstream(services: Settings) -> None:
    log = FakeLlmLog()
    with gateway(services, new_api_key(services), log) as client:
        client.post("/v1/chat/completions", json=HELLO)

    [upstream_request] = log.requests
    assert upstream_request["max_tokens"] == services.default_max_tokens


def test_a_request_the_budget_cannot_cover_gets_a_429(services: Settings) -> None:
    settings = services.model_copy(update={"token_budget_per_day": 100})
    with gateway(settings, new_api_key(services)) as client:
        response = client.post("/v1/chat/completions", json=HELLO | {"max_tokens": 200})

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "insufficient_quota"


def test_redis_down_fails_closed_while_budgets_are_on(services: Settings) -> None:
    api_key = new_api_key(services)
    down = services.model_copy(update={"redis_url": unused_local_url().replace("http", "redis")})
    with gateway(down, api_key) as client:
        response = client.post("/v1/chat/completions", json=HELLO)

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "service_unavailable"


def test_redis_down_without_budgets_is_still_rate_limited_per_replica(
    services: Settings,
) -> None:
    api_key = new_api_key(services)
    down = services.model_copy(
        update={
            "redis_url": unused_local_url().replace("http", "redis"),
            "token_budget_per_day": None,
            "rate_limit_burst": 1,
            "rate_limit_per_s": 0.1,
        }
    )
    with gateway(down, api_key) as client:
        statuses = [client.post("/v1/chat/completions", json=HELLO).status_code for _ in range(2)]

    assert statuses == [200, 429]
