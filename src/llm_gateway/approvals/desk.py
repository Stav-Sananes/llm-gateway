"""The admin's side of approvals: see what's waiting, approve or reject it."""

from collections.abc import Callable, Sequence
from datetime import datetime
from uuid import UUID

from llm_gateway.approvals.hold import utc_now
from llm_gateway.approvals.model import GATEWAY, Approval, ApprovalStatus
from llm_gateway.approvals.ports import ApprovalStore


class ApprovalNotFoundError(Exception):
    pass


class ApprovalNotPendingError(Exception):
    """Already decided, expired, or abandoned: a decision now would change nothing."""


class ApprovalDesk:
    def __init__(self, store: ApprovalStore, clock: Callable[[], datetime] = utc_now) -> None:
        self._store = store
        self._clock = clock

    async def waiting(self) -> Sequence[Approval]:
        return await self._store.pending(self._clock())

    async def show(self, approval_id: UUID) -> Approval:
        approval = await self._store.get(approval_id)
        if approval is None:
            raise ApprovalNotFoundError(f"no approval {approval_id}")
        return approval

    async def approve(self, approval_id: UUID, by: str, note: str | None = None) -> Approval:
        return await self._decide(approval_id, ApprovalStatus.APPROVED, by, note)

    async def reject(self, approval_id: UUID, by: str, note: str | None = None) -> Approval:
        return await self._decide(approval_id, ApprovalStatus.REJECTED, by, note)

    async def _decide(
        self, approval_id: UUID, status: ApprovalStatus, by: str, note: str | None
    ) -> Approval:
        if not by.strip() or by.strip().casefold() == GATEWAY:
            raise ValueError("a decision needs the name of the person making it")
        approval = await self.show(approval_id)
        now = self._clock()
        if approval.status is ApprovalStatus.PENDING and approval.expires_at <= now:
            # Its client has stopped waiting (or its gateway died): approving runs nothing.
            await self._store.decide(approval_id, ApprovalStatus.EXPIRED, GATEWAY, now)
            raise ApprovalNotPendingError(f"approval {approval_id} has expired")
        decided = await self._store.decide(approval_id, status, by, now, note)
        if decided is None:
            current = await self.show(approval_id)
            raise ApprovalNotPendingError(f"approval {approval_id} is already {current.status}")
        return decided
