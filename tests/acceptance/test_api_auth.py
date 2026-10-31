"""Bearer-key auth on the API, end to end: real gateway, Postgres, Redis and a fake LLM."""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest

from harness.fake_llm import FakeLlmScript, create_fake_llm
from llm_gateway.config import Settings
from llm_gateway.main import build_app, build_redis
from llm_gateway.tenancy.keys import hash_key
from tests.support.backing_services import new_api_key, revoke
from tests.support.servers import serving, unused_local_url

HELLO = {"model": "fake-model", "messages": [{"role": "user", "content": "hi"}]}


@contextmanager
def gateway(settings: Settings) -> Iterator[httpx.Client]:
    with serving(create_fake_llm(FakeLlmScript())) as upstream_url:
        settings = settings.model_copy(update={"upstream_base_url": f"{upstream_url}/v1"})
        with serving(build_app(settings)) as url, httpx.Client(base_url=url) as client:
            yield client


def chat(client: httpx.Client, api_key: str) -> httpx.Response:
    return client.post(
        "/v1/chat/completions", json=HELLO, headers={"Authorization": f"Bearer {api_key}"}
    )


def with_unreachable_postgres(settings: Settings) -> Settings:
    port = unused_local_url().rsplit(":", 1)[1]
    url = f"postgresql+asyncpg://gateway:gateway@127.0.0.1:{port}/gateway"
    return settings.model_copy(update={"database_url": url})


def with_unreachable_redis(settings: Settings) -> Settings:
    url = unused_local_url().replace("http://", "redis://")
    return settings.model_copy(update={"redis_url": url})


def cache_ttl_s(settings: Settings, api_key: str) -> int:
    async def ttl() -> int:
        redis = build_redis(settings)
        try:
            return int(await redis.ttl(f"gw:key:{hash_key(api_key)}"))
        finally:
            await redis.aclose()

    return asyncio.run(ttl())


def assert_invalid_api_key(response: httpx.Response) -> None:
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json()["error"] == {
        "message": "Invalid API key.",
        "type": "invalid_request_error",
        "code": "invalid_api_key",
    }


def test_valid_key_is_served(services: Settings) -> None:
    api_key = new_api_key(services)
    with gateway(services) as client:
        assert chat(client, api_key).status_code == 200


def test_request_without_a_key_is_a_401(services: Settings) -> None:
    with gateway(services) as client:
        assert_invalid_api_key(client.post("/v1/chat/completions", json=HELLO))


@pytest.mark.parametrize("authorization", ["Basic dXNlcjpwYXNz", "Bearer"])
def test_request_without_a_bearer_key_is_a_401(services: Settings, authorization: str) -> None:
    with gateway(services) as client:
        response = client.post(
            "/v1/chat/completions", json=HELLO, headers={"Authorization": authorization}
        )

    assert_invalid_api_key(response)


@pytest.mark.parametrize("api_key", ["sk-not-ours", "gw_deadbeef_" + "x" * 43])
def test_unknown_key_is_a_401(services: Settings, api_key: str) -> None:
    with gateway(services) as client:
        assert_invalid_api_key(chat(client, api_key))


def test_revoked_key_is_a_401_even_after_it_was_cached(services: Settings) -> None:
    api_key = new_api_key(services)
    with gateway(services) as client:
        assert chat(client, api_key).status_code == 200

        revoke(services, api_key)

        assert_invalid_api_key(chat(client, api_key))


def test_gateway_caches_keys_for_the_configured_ttl(services: Settings) -> None:
    api_key = new_api_key(services)
    with gateway(services) as client:
        chat(client, api_key)

    assert 0 < cache_ttl_s(services, api_key) <= services.key_cache_ttl_s


def test_health_check_needs_no_key(services: Settings) -> None:
    with gateway(services) as client:
        assert client.get("/healthz").status_code == 200


def test_redis_down_still_serves_valid_keys_from_postgres(services: Settings) -> None:
    api_key = new_api_key(services)
    with gateway(with_unreachable_redis(services)) as client:
        assert chat(client, api_key).status_code == 200


def test_postgres_down_still_serves_cached_keys(services: Settings) -> None:
    api_key = new_api_key(services)
    with gateway(services) as client:
        assert chat(client, api_key).status_code == 200  # now cached in Redis

    with gateway(with_unreachable_postgres(services)) as client:
        assert chat(client, api_key).status_code == 200


def test_postgres_down_and_key_not_cached_is_a_503(services: Settings) -> None:
    api_key = new_api_key(services)
    with gateway(with_unreachable_postgres(services)) as client:
        response = chat(client, api_key)

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "service_unavailable"
