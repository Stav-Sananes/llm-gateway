from collections.abc import Iterator

import pytest

from tests.support.postgres import migrated_postgres


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """One migrated Postgres container per test run, started only if a test asks for it."""
    with migrated_postgres() as url:
        yield url
