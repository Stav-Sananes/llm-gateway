"""The audit use cases: record an event, and verify a tenant's chain."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from llm_gateway.audit.chain import EMPTY_CHAIN, AuditEvent, AuditRecord, link_hash
from llm_gateway.audit.ports import AuditStore
from llm_gateway.audit.redaction import redact


@dataclass(frozen=True)
class Verification:
    ok: bool
    checked: int
    head_hash: str
    problem: str | None = None  # e.g. "seq 7: hash does not match its content"


class AuditLog:
    def __init__(self, store: AuditStore) -> None:
        self._store = store

    async def record(self, tenant_id: UUID, action: str, details: Mapping[str, Any]) -> AuditRecord:
        """Redact, then append. Raises AuditUnavailableError: callers must not proceed."""
        event = AuditEvent(
            tenant_id=tenant_id,
            action=action,
            occurred_at=datetime.now(UTC),
            details=redact(details),
        )
        return await self._store.append(event)

    async def verify(self, tenant_id: UUID) -> Verification:
        """Recompute the whole chain. Catches edits, deletions, reordering and a cut-off tail."""
        expected_prev, expected_seq, checked = EMPTY_CHAIN.hash, 1, 0
        async for record in self._store.records(tenant_id):
            problem = _problem(record, expected_seq, expected_prev)
            if problem:
                return Verification(False, checked, expected_prev, f"seq {expected_seq}: {problem}")
            expected_prev, expected_seq, checked = record.hash, record.seq + 1, checked + 1
        head = await self._store.head(tenant_id)
        if (head.seq, head.hash) != (checked, expected_prev):
            problem = f"chain ends at seq {checked} but its head says seq {head.seq}"
            return Verification(False, checked, expected_prev, problem)
        return Verification(True, checked, expected_prev)


def _problem(record: AuditRecord, expected_seq: int, expected_prev: str) -> str | None:
    if record.seq != expected_seq:
        return f"missing; next stored record is seq {record.seq}"
    if record.prev_hash != expected_prev:
        return "does not link to the record before it"
    if record.hash != link_hash(record.prev_hash, record.payload):
        return "hash does not match its content"
    return None
