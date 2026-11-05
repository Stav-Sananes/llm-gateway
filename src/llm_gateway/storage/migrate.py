"""Runs the Alembic migrations shipped inside the package, so the container image can migrate."""

import asyncio
from dataclasses import dataclass

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

# Waits bounded everywhere: connecting, and queueing for a table lock (see migrations/env.py).
CONNECT_TIMEOUT_S = 10


@dataclass(frozen=True)
class Migrated:
    """The schema is at this image's newest revision."""


@dataclass(frozen=True)
class SchemaAhead:
    """The database is at revisions this image doesn't know: a newer release migrated it,
    then was rolled back. Nothing was changed; the newer schema must stay compatible."""

    revisions: frozenset[str]


def upgrade_to_head(database_url: str) -> Migrated | SchemaAhead:
    """Bring the schema up to date. Blocking: call it outside a running event loop."""
    config = _config(database_url)
    known = {script.revision for script in ScriptDirectory.from_config(config).walk_revisions()}
    unknown = frozenset(asyncio.run(_current_revisions(database_url)) - known)
    if unknown:
        return SchemaAhead(unknown)
    command.upgrade(config, "head")
    return Migrated()


def _config(database_url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", "llm_gateway.storage:migrations")
    # Passed as an attribute, not a main option: ini values treat "%" in passwords as interpolation.
    config.attributes["database_url"] = database_url
    return config


async def _current_revisions(database_url: str) -> set[str]:
    engine = create_async_engine(
        database_url, poolclass=pool.NullPool, connect_args={"timeout": CONNECT_TIMEOUT_S}
    )
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_heads)
    finally:
        await engine.dispose()


def _heads(connection: Connection) -> set[str]:
    return set(MigrationContext.configure(connection).get_current_heads())
