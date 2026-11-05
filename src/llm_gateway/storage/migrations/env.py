"""Alembic environment. llm_gateway.storage.migrate passes the database URL in; no .ini file."""

import asyncio

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from llm_gateway.storage.migrate import CONNECT_TIMEOUT_S
from llm_gateway.storage.tables import metadata

# The gateway keeps serving while this runs (a Helm pre-upgrade hook). A migration that
# queued for a table lock would make every gateway query queue behind it; this makes it
# fail instead, and the Job retries.
LOCK_TIMEOUT = "5s"


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online(database_url: str) -> None:
    engine = create_async_engine(
        database_url,
        poolclass=pool.NullPool,
        # A session setting, not a SET statement: a statement would open a transaction
        # that Alembic then treats as the caller's, and never commits.
        connect_args={
            "timeout": CONNECT_TIMEOUT_S,
            "server_settings": {"lock_timeout": LOCK_TIMEOUT},
        },
    )
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


asyncio.run(_run_online(context.config.attributes["database_url"]))
