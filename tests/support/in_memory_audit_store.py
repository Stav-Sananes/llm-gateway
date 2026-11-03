"""List-backed AuditStore for tests, with hooks to tamper with it like an attacker would.

Unlike Postgres it doesn't page, and it hands back the very records it was given; paging
and the stored-bytes round trip are only proven by the Postgres runs of the contract tests.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import UUID

from llm_gateway.audit.chain import EMPTY_CHAIN, AuditEvent, AuditRecord, ChainHead, next_record


class InMemoryAuditStore:
    def __init__(self) -> None:
        self._records: dict[UUID, list[AuditRecord]] = {}
        self._heads: dict[UUID, ChainHead] = {}
        self._lock = asyncio.Lock()

    async def append(self, event: AuditEvent) -> AuditRecord:
        async with self._lock:
            head = self._heads.get(event.tenant_id, EMPTY_CHAIN)
            await asyncio.sleep(0)  # a suspension point: without the lock, appends would fork
            record = next_record(head, event)
            self._records.setdefault(event.tenant_id, []).append(record)
            self._heads[event.tenant_id] = ChainHead(record.seq, record.hash)
            return record

    async def records(self, tenant_id: UUID, up_to_seq: int) -> AsyncIterator[AuditRecord]:
        for record in list(self._records.get(tenant_id, [])):
            if record.seq <= up_to_seq:
                yield record

    async def head(self, tenant_id: UUID) -> ChainHead:
        return self._heads.get(tenant_id, EMPTY_CHAIN)

    # Tampering, for tests.
    def edit_payload(self, tenant_id: UUID, seq: int, payload: str) -> None:
        records = self._records[tenant_id]
        records[seq - 1] = replace(records[seq - 1], payload=payload)

    def delete(self, tenant_id: UUID, seq: int) -> None:
        del self._records[tenant_id][seq - 1]
