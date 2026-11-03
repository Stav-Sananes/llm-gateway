"""The audit use cases: record an event, and verify a tenant's chain."""

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from llm_gateway.audit.chain import EMPTY_CHAIN, AuditEvent, AuditRecord, canonical, link_hash
from llm_gateway.audit.ports import AuditStore
from llm_gateway.audit.redaction import redact


@dataclass(frozen=True)
class Verification:
    ok: bool
    checked: int
    head_hash: str
    problem: str | None = None  # e.g. "seq 7: hash does not match its content"


class AuditLog:
    def __init__(self, store: AuditStore, secret_keys: Iterable[str] = ()) -> None:
        self._store = store
        self._secret_keys = tuple(secret_keys)

    async def record(self, tenant_id: UUID, action: str, details: Mapping[str, Any]) -> AuditRecord:
        """Redact, then append. Raises AuditUnavailableError or UnauditableEventError:
        either way the audited action must not proceed."""
        event = AuditEvent(
            tenant_id=tenant_id,
            action=action,
            occurred_at=datetime.now(UTC),
            details=redact(details, self._secret_keys),
        )
        canonical(event)  # fail on an unrenderable event before touching the store
        return await self._store.append(event)

    async def verify(self, tenant_id: UUID) -> Verification:
        """Recompute the chain up to its head. Catches edits, deletions, reordering, records
        moved between tenants and a cut-off tail. The head is read first, so a tenant that is
        still writing verifies cleanly up to that point."""
        head = await self._store.head(tenant_id)
        expected_prev, expected_seq, checked = EMPTY_CHAIN.hash, 1, 0
        async for record in self._store.records(tenant_id, up_to_seq=head.seq):
            problem = _problem(record, tenant_id, expected_seq, expected_prev)
            if problem:
                return Verification(False, checked, expected_prev, f"seq {expected_seq}: {problem}")
            expected_prev, expected_seq, checked = record.hash, record.seq + 1, checked + 1
        if checked != head.seq:
            problem = f"chain ends at seq {checked} but its head says seq {head.seq}"
            return Verification(False, checked, expected_prev, problem)
        if expected_prev != head.hash:
            problem = f"head hash does not match the hash of seq {checked}"
            return Verification(False, checked, expected_prev, problem)
        return Verification(True, checked, expected_prev)


def _problem(
    record: AuditRecord, tenant_id: UUID, expected_seq: int, expected_prev: str
) -> str | None:
    if record.seq != expected_seq:
        return f"missing; next stored record is seq {record.seq}"
    if record.prev_hash != expected_prev:
        return "does not link to the record before it"
    if record.hash != link_hash(record.prev_hash, record.payload):
        return "hash does not match its content"
    if _payload_tenant(record.payload) != str(tenant_id):
        return "belongs to another tenant"
    return None


def _payload_tenant(payload: str) -> object:
    try:
        return json.loads(payload).get("tenant_id")
    except (ValueError, AttributeError):
        return None
