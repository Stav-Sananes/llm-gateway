"""Postgres-backed UsageStore: batched, idempotent inserts into usage_events."""

from collections.abc import Sequence
from dataclasses import asdict

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DataError, IntegrityError, SQLAlchemyError
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
        except (IntegrityError, DataError) as error:
            raise InvalidUsageEventError(type(error).__name__) from error
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise UsageStoreUnavailableError(type(error).__name__) from error
