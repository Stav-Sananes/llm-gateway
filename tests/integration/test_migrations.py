import time
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from testcontainers.community.postgres import PostgresContainer

from llm_gateway.storage.migrate import Migrated, SchemaAhead, upgrade_to_head
from llm_gateway.storage.tables import metadata
from tests.support.postgres import POSTGRES_IMAGE

pytestmark = pytest.mark.anyio


def _schema_differences(connection: Connection) -> list[Any]:
    return list(compare_metadata(MigrationContext.configure(connection), metadata))


async def test_migrations_build_exactly_the_schema_the_code_expects(postgres_url: str) -> None:
    engine = create_async_engine(postgres_url)
    try:
        async with engine.connect() as connection:
            differences = await connection.run_sync(_schema_differences)
    finally:
        await engine.dispose()

    assert differences == []


def migrate(url: str) -> Migrated | SchemaAhead:
    # Alembic runs its own event loop: keep it off the test's.
    with ThreadPoolExecutor(max_workers=1) as worker:
        return worker.submit(upgrade_to_head, url).result()


async def test_an_up_to_date_database_is_left_up_to_date(postgres_url: str) -> None:
    assert migrate(postgres_url) == Migrated()


async def test_a_database_migrated_by_a_newer_release_is_left_alone(postgres_url: str) -> None:
    # After `helm rollback`: the older release's pre-upgrade hook must not fail on the
    # newer schema, or every later upgrade of the older release is blocked.
    engine = create_async_engine(postgres_url)
    try:
        async with engine.begin() as connection:
            current = (
                await connection.exec_driver_sql("SELECT version_num FROM alembic_version")
            ).scalar_one()
            await connection.exec_driver_sql(
                "UPDATE alembic_version SET version_num = '9999_newer'"
            )
        try:
            outcome = migrate(postgres_url)
            async with engine.connect() as connection:
                after = (
                    await connection.exec_driver_sql("SELECT version_num FROM alembic_version")
                ).scalar_one()
        finally:
            async with engine.begin() as connection:
                await connection.exec_driver_sql(
                    "UPDATE alembic_version SET version_num = $1", (current,)
                )
    finally:
        await engine.dispose()

    assert outcome == SchemaAhead(frozenset({"9999_newer"}))
    assert after == "9999_newer"


@pytest.fixture(scope="module")
def fresh_postgres() -> Iterator[str]:
    """A Postgres of its own: these tests migrate it down and hold locks on it."""
    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as container:
        yield container.get_connection_url()


def migrate_to(url: str, revision: str) -> None:
    config = Config()
    config.set_main_option("script_location", "llm_gateway.storage:migrations")
    config.attributes["database_url"] = url
    with ThreadPoolExecutor(max_workers=1) as worker:
        worker.submit(command.upgrade, config, revision).result()


async def version(url: str) -> str:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            result = await connection.exec_driver_sql("SELECT version_num FROM alembic_version")
            return str(result.scalar_one())
    finally:
        await engine.dispose()


async def test_a_database_one_release_behind_is_brought_up_to_date(fresh_postgres: str) -> None:
    # The everyday upgrade: an older schema must be migrated, never mistaken for a newer one.
    migrate_to(fresh_postgres, "0003")

    assert migrate(fresh_postgres) == Migrated()
    assert await version(fresh_postgres) == "0004"


async def test_a_migration_waiting_for_a_table_lock_gives_up_instead_of_queueing(
    fresh_postgres: str,
) -> None:
    # The gateway keeps serving during the upgrade: a migration queued behind a lock would
    # queue every gateway query behind it.
    async with drop_to_0003(fresh_postgres):
        engine = create_async_engine(fresh_postgres)
        try:
            async with engine.connect() as holder:
                await holder.exec_driver_sql("BEGIN")
                await holder.exec_driver_sql("LOCK TABLE tenants IN ACCESS EXCLUSIVE MODE")
                started = time.monotonic()
                with pytest.raises(DBAPIError, match="lock timeout"):
                    migrate(fresh_postgres)
                waited = time.monotonic() - started
                await holder.exec_driver_sql("ROLLBACK")
        finally:
            await engine.dispose()

    assert 4 < waited < 15  # LOCK_TIMEOUT is 5 s


@asynccontextmanager
async def drop_to_0003(url: str) -> AsyncIterator[None]:
    """At 0003, the next migration (0004) needs a lock on tenants; back at head after."""
    config = Config()
    config.set_main_option("script_location", "llm_gateway.storage:migrations")
    config.attributes["database_url"] = url
    migrate_to(url, "head")
    with ThreadPoolExecutor(max_workers=1) as worker:
        worker.submit(command.downgrade, config, "0003").result()
    try:
        yield
    finally:
        migrate_to(url, "head")
