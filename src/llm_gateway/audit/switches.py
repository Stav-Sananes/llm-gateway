"""Provider switches mid-answer, recorded on the tenant's audit chain."""

import logging
from uuid import UUID

from llm_gateway.audit.chain import UnauditableEventError
from llm_gateway.audit.log import AuditLog
from llm_gateway.audit.ports import AuditUnavailableError
from llm_gateway.chat.model import Origin

logger = logging.getLogger(__name__)

SWITCHED = "chat.provider_switched"


class AuditedSwitches:
    """The chat use case's ProviderSwitches. The answer was already served, so an audit
    store that can't take the record costs the record, not the request."""

    def __init__(self, audit: AuditLog) -> None:
        self._audit = audit

    async def record(
        self, tenant_id: UUID, requested_model: str, dead: Origin, resumed: Origin
    ) -> None:
        details = {
            "requested_model": requested_model,
            "from": {"provider": dead.provider, "model": dead.model},
            "to": {"provider": resumed.provider, "model": resumed.model},
        }
        try:
            await self._audit.record(tenant_id, SWITCHED, details)
        except (AuditUnavailableError, UnauditableEventError) as error:
            logger.error("provider switch not audited for tenant %s: %s", tenant_id, error)
