"""Postgres-backed UsageStore: batched, idempotent inserts into usage_events."""

from collections.abc import Sequence
from dataclasses import asdict

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from llm_gateway.storage.tables import usage_events
from llm_gateway.usage.model import UsageEvent
from llm_gateway.usage.ports import InvalidUsageEventError, UsageStoreUnavailableError


class PostgresUsageStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert_many(self, events: Sequence[UsageEvent]) -> None:
        if not events:
            return
        rows = [asdict(event) | {"outcome": event.outcome.value} for event in events]
        # A redelivered event is already there: skip it, don't fail the batch.
        statement = (
            insert(usage_events).values(rows).on_conflict_do_nothing(index_elements=["event_id"])
        )
        try:
            async with self._engine.begin() as connection:
                await connection.execute(statement)
        except DBAPIError as error:
            if _refuses_the_data(error):
                raise InvalidUsageEventError(_describe(error)) from error
            raise UsageStoreUnavailableError(_describe(error)) from error
        except (SQLAlchemyError, OSError, TimeoutError, UnicodeError) as error:
            if isinstance(error, UnicodeError):  # a value that can't be sent at all
                raise InvalidUsageEventError(type(error).__name__) from error
            raise UsageStoreUnavailableError(type(error).__name__) from error

    async def reachable(self) -> bool:
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError, TimeoutError):
            return False
        return True


def _refuses_the_data(error: DBAPIError) -> bool:
    """Postgres error classes 22 (data exception) and 23 (integrity constraint) are about
    the rows, not the connection. asyncpg also refuses some values before sending them."""
    code = getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)
    if isinstance(code, str):
        return code[:2] in {"22", "23"}
    return "invalid input for query argument" in str(error.orig)


def _describe(error: DBAPIError) -> str:
    return f"{type(error.orig).__name__}: {str(error.orig).splitlines()[0][:200]}"
