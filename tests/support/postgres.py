"""A throwaway, migrated Postgres for tests, plus helpers to reset it."""

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from sqlalchemy.ext.asyncio import AsyncEngine
from testcontainers.community.postgres import PostgresContainer

from llm_gateway.storage.migrate import upgrade_to_head

POSTGRES_IMAGE = "postgres:18.6-alpine"


@contextmanager
def migrated_postgres() -> Iterator[str]:
    """Start Postgres in Docker, run our migrations, yield its asyncpg URL."""
    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as container:
        url = container.get_connection_url()
        # Alembic runs its own event loop; a worker thread keeps it clear of the test's loop.
        with ThreadPoolExecutor(max_workers=1) as worker:
            worker.submit(upgrade_to_head, url).result()
        yield url


async def empty_tables(engine: AsyncEngine) -> None:
    """Clean at the start of each test, so a failed test leaves evidence behind."""
    async with engine.begin() as connection:
        await connection.exec_driver_sql("TRUNCATE api_keys, tenants")
