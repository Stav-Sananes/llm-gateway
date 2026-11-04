"""List-backed ApprovalStore for tests, with the same rules as Postgres."""

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from uuid import UUID

import anyio

from llm_gateway.approvals.model import Approval, ApprovalStatus
from llm_gateway.approvals.ports import ApprovalUnavailableError, TooManyPendingError


class InMemoryApprovalStore:
    def __init__(self) -> None:
        self.approvals: dict[UUID, Approval] = {}
        self.down = False
        self.held = anyio.Event()  # set once anything is held: a test's cue to decide

    async def hold(self, approval: Approval, max_pending: int, now: datetime) -> None:
        await self._round_trip()
        waiting = [
            a
            for a in self.approvals.values()
            if a.tenant_id == approval.tenant_id
            and a.status is ApprovalStatus.PENDING
            and a.expires_at > now
        ]
        if len(waiting) >= max_pending:
            raise TooManyPendingError(f"{len(waiting)} calls already wait for approval")
        self.approvals[approval.id] = approval
        self.held.set()

    async def get(self, approval_id: UUID) -> Approval | None:
        await self._round_trip()
        return self.approvals.get(approval_id)

    async def decide(
        self,
        approval_id: UUID,
        status: ApprovalStatus,
        by: str,
        at: datetime,
        note: str | None = None,
    ) -> Approval | None:
        await self._round_trip()
        current = self.approvals.get(approval_id)
        if current is None or current.status is not ApprovalStatus.PENDING:
            return None
        decided = replace(current, status=status, decided_at=at, decided_by=by, note=note)
        self.approvals[approval_id] = decided
        return decided

    async def pending(self, now: datetime) -> Sequence[Approval]:
        await self._round_trip()
        waiting = [
            a
            for a in self.approvals.values()
            if a.status is ApprovalStatus.PENDING and a.expires_at > now
        ]
        return sorted(waiting, key=lambda a: a.requested_at)

    async def _round_trip(self) -> None:
        await anyio.lowlevel.checkpoint()  # like a network round trip, yields to the loop
        if self.down:
            raise ApprovalUnavailableError("postgres down")
