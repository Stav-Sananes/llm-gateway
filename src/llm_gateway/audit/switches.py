"""Provider switches mid-answer, recorded on the tenant's audit chain, off the response path.

The answer has already been served when a switch is recorded, so the record must not hold
the response up: during a provider outage every in-flight stream resumes at once, and
appending inline would queue them all on the audit store, ahead of their [DONE], and next
to the tool-call audits that do have to happen first. record() only queues; run() appends
one at a time. A full queue (the audit store down or slow) drops the record, logged.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

import anyio

from llm_gateway.audit.chain import UnauditableEventError
from llm_gateway.audit.log import AuditLog
from llm_gateway.audit.ports import AuditUnavailableError
from llm_gateway.chat.model import Origin

logger = logging.getLogger(__name__)

SWITCHED = "chat.provider_switched"


@dataclass(frozen=True)
class _Switch:
    tenant_id: UUID
    requested_model: str
    dead: Origin
    resumed: Origin


class AuditedSwitches:
    """The chat use case's ProviderSwitches."""

    def __init__(self, audit: AuditLog, queue_size: int = 100) -> None:
        self._audit = audit
        self._queue_in, self._queue_out = anyio.create_memory_object_stream[_Switch](queue_size)

    async def record(
        self, tenant_id: UUID, requested_model: str, dead: Origin, resumed: Origin
    ) -> None:
        try:
            self._queue_in.send_nowait(_Switch(tenant_id, requested_model, dead, resumed))
        except anyio.WouldBlock:
            logger.error("provider switch not audited for tenant %s: queue full", tenant_id)

    async def run(self) -> None:
        """Append queued switches until cancelled. The app's lifespan runs it."""
        async for switch in self._queue_out:
            await self._append(switch)

    async def write_pending(self) -> None:
        """Append whatever is queued now (tests)."""
        while True:
            try:
                switch = self._queue_out.receive_nowait()
            except anyio.WouldBlock:
                return
            await self._append(switch)

    async def _append(self, switch: _Switch) -> None:
        details = {
            "requested_model": switch.requested_model,
            "from": {"provider": switch.dead.provider, "model": switch.dead.model},
            "to": {"provider": switch.resumed.provider, "model": switch.resumed.model},
        }
        try:
            await self._audit.record(switch.tenant_id, SWITCHED, details)
        except (AuditUnavailableError, UnauditableEventError) as error:
            logger.error("provider switch not audited for tenant %s: %s", switch.tenant_id, error)
