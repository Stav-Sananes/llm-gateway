from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from llm_gateway.storage.migrate import Migrated, SchemaAhead, upgrade_to_head
from llm_gateway.storage.tables import metadata

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
