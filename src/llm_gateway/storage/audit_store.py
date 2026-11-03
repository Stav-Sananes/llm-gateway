"""Postgres-backed AuditStore. Same contract as the in-memory fake; see tests/contract."""

from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from llm_gateway.audit.chain import EMPTY_CHAIN, AuditEvent, AuditRecord, ChainHead, next_record
from llm_gateway.audit.ports import AuditUnavailableError
from llm_gateway.storage.tables import audit_events, audit_heads


class PostgresAuditStore:
    def __init__(self, engine: AsyncEngine, batch_size: int = 1000) -> None:
        self._engine = engine
        self._batch_size = batch_size  # verify reads the chain in pages, never all at once

    async def append(self, event: AuditEvent) -> AuditRecord:
        try:
            async with self._engine.begin() as connection:
                head = await _locked_head(connection, event.tenant_id)
                record = next_record(head, event)
                await connection.execute(
                    insert(audit_events).values(
                        tenant_id=record.tenant_id,
                        seq=record.seq,
                        prev_hash=record.prev_hash,
                        hash=record.hash,
                        payload=record.payload,
                    )
                )
                await connection.execute(
                    update(audit_heads)
                    .where(audit_heads.c.tenant_id == event.tenant_id)
                    .values(seq=record.seq, hash=record.hash)
                )
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise AuditUnavailableError(type(error).__name__) from error
        return record

    async def records(self, tenant_id: UUID, up_to_seq: int) -> AsyncIterator[AuditRecord]:
        after = 0
        while True:
            query = (
                select(audit_events)
                .where(
                    audit_events.c.tenant_id == tenant_id,
                    audit_events.c.seq > after,
                    audit_events.c.seq <= up_to_seq,
                )
                .order_by(audit_events.c.seq)
                .limit(self._batch_size)
            )
            async with self._engine.connect() as connection:
                rows = (await connection.execute(query)).all()
            for row in rows:
                yield AuditRecord(row.tenant_id, row.seq, row.prev_hash, row.hash, row.payload)
            if len(rows) < self._batch_size:
                return
            after = rows[-1].seq

    async def head(self, tenant_id: UUID) -> ChainHead:
        query = select(audit_heads.c.seq, audit_heads.c.hash).where(
            audit_heads.c.tenant_id == tenant_id
        )
        async with self._engine.connect() as connection:
            row = (await connection.execute(query)).one_or_none()
        return EMPTY_CHAIN if row is None else ChainHead(seq=row.seq, hash=row.hash)


async def _locked_head(connection: AsyncConnection, tenant_id: UUID) -> ChainHead:
    # Create the head row on a tenant's first event, then lock it: concurrent appends for
    # this tenant wait here, so each one links onto the true latest hash.
    await connection.execute(
        pg_insert(audit_heads)
        .values(tenant_id=tenant_id, seq=EMPTY_CHAIN.seq, hash=EMPTY_CHAIN.hash)
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )
    row = (
        await connection.execute(
            select(audit_heads.c.seq, audit_heads.c.hash)
            .where(audit_heads.c.tenant_id == tenant_id)
            .with_for_update()
        )
    ).one()
    return ChainHead(seq=row.seq, hash=row.hash)
