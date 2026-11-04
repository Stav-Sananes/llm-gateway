"""What approvals need from storage."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol
from uuid import UUID

from llm_gateway.approvals.model import Approval, ApprovalStatus


class ApprovalUnavailableError(Exception):
    """The approval store could not be reached: the held call must not run."""


class TooManyPendingError(Exception):
    """The tenant already has as many calls waiting for approval as it may."""


class ApprovalStore(Protocol):
    async def hold(self, approval: Approval, max_pending: int, now: datetime) -> None:
        """Park a pending approval. Raises TooManyPendingError when its tenant already has
        max_pending approvals pending and not yet expired at `now`."""
        ...

    async def get(self, approval_id: UUID) -> Approval | None: ...

    async def decide(
        self,
        approval_id: UUID,
        status: ApprovalStatus,
        by: str,
        at: datetime,
        note: str | None = None,
    ) -> Approval | None:
        """Move a pending approval to `status`, atomically. None when it was no longer
        pending: someone else decided first, and their decision stands."""
        ...

    async def pending(self, now: datetime) -> Sequence[Approval]:
        """Every tenant's pending approvals not yet expired at `now`, oldest first."""
        ...
