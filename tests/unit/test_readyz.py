"""/readyz: ready while any provider can take a request."""

from uuid import uuid4

from fastapi.testclient import TestClient

from llm_gateway.chat.use_case import ChatCompletions
from llm_gateway.http.app import create_app
from llm_gateway.limits.token_bucket import InMemoryRateLimiter
from llm_gateway.routing.breaker import CircuitBreaker
from llm_gateway.tenancy.auth import Authenticator
from tests.support.in_memory_key_cache import InMemoryKeyCache
from tests.support.in_memory_tenant_store import InMemoryTenantStore
from tests.support.limits import InMemoryTokenBudget
from tests.support.providers import ScriptedProvider


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def readyz(breakers: list[CircuitBreaker]) -> TestClient:
    chat = ChatCompletions(
        ScriptedProvider(()), InMemoryRateLimiter(1, 1.0), InMemoryTokenBudget(1), 1
    )
    app = create_app(chat, Authenticator(InMemoryTenantStore(), InMemoryKeyCache()), breakers)
    return TestClient(app)


def test_a_half_open_provider_counts_as_able_to_take_requests() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(f"p-{uuid4()}", 1, 10.0, clock)
    breaker.record_failure()
    clock.now += 10

    response = readyz([breaker]).get("/readyz")

    assert response.status_code == 200
    assert response.json()["providers"] == {breaker.name: "half_open"}


def test_no_providers_configured_is_not_a_reason_to_be_unready() -> None:
    assert readyz([]).get("/readyz").status_code == 200
