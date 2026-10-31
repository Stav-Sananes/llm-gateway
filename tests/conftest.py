from collections.abc import Iterator

import pytest

from llm_gateway.config import Settings
from tests.support.backing_services import reset
from tests.support.postgres import migrated_postgres
from tests.support.redis import running_redis


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """One migrated Postgres container per test run, started only if a test asks for it."""
    with migrated_postgres() as url:
        yield url


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    """One Redis container per test run, started only if a test asks for it."""
    with running_redis() as url:
        yield url


@pytest.fixture
def services(postgres_url: str, redis_url: str, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings pointing at the test Postgres and Redis, both emptied first.

    Also exported as GATEWAY_* variables, for code that reads Settings() itself (the CLI).
    """
    monkeypatch.setenv("GATEWAY_DATABASE_URL", postgres_url)
    monkeypatch.setenv("GATEWAY_REDIS_URL", redis_url)
    settings = Settings()
    reset(settings)
    return settings
