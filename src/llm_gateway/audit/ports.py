"""What the audit log needs from storage."""

from collections.abc import AsyncIterator
from typing import Protocol
from uuid import UUID

from llm_gateway.audit.chain import AuditEvent, AuditRecord, ChainHead


class AuditUnavailableError(Exception):
    """The audit store could not be reached; the audited action must not proceed."""


class AuditStore(Protocol):
    async def append(self, event: AuditEvent) -> AuditRecord:
        """Link the event onto its tenant's chain and store it, atomically.

        Concurrent appends for one tenant must be serialized, so the chain stays a line.
        Raises AuditUnavailableError when the store can't be reached.
        """
        ...

    def records(self, tenant_id: UUID, up_to_seq: int) -> AsyncIterator[AuditRecord]:
        """The tenant's records with seq <= up_to_seq, in seq order, read in bounded batches."""
        ...

    async def head(self, tenant_id: UUID) -> ChainHead:
        """The tenant's recorded chain head (EMPTY_CHAIN if it has no events)."""
        ...
