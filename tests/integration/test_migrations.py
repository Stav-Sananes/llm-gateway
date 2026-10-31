from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

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
