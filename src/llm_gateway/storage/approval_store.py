"""Postgres-backed ApprovalStore. Same contract as the in-memory fake; see tests/contract."""

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.engine import Row
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from llm_gateway.approvals.model import Approval, ApprovalStatus
from llm_gateway.approvals.ports import ApprovalUnavailableError, TooManyPendingError
from llm_gateway.storage.tables import approvals

_UNAVAILABLE = (SQLAlchemyError, OSError, TimeoutError)


class PostgresApprovalStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def hold(self, approval: Approval, max_pending: int, now: datetime) -> None:
        waiting = (
            select(func.count())
            .select_from(approvals)
            .where(
                approvals.c.tenant_id == approval.tenant_id,
                approvals.c.status == ApprovalStatus.PENDING.value,
                approvals.c.expires_at > now,
            )
        )
        try:
            async with self._engine.begin() as connection:
                # One tenant's holds are counted and added one at a time, so concurrent
                # calls can't all slip under the limit together.
                await connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:tenant, 0))"),
                    {"tenant": f"approvals:{approval.tenant_id}"},
                )
                if (await connection.execute(waiting)).scalar_one() >= max_pending:
                    raise TooManyPendingError(f"{max_pending} calls already wait for approval")
                await connection.execute(insert(approvals).values(**_columns(approval)))
        except _UNAVAILABLE as error:
            raise ApprovalUnavailableError(type(error).__name__) from error

    async def get(self, approval_id: UUID) -> Approval | None:
        query = select(approvals).where(approvals.c.id == approval_id)
        try:
            async with self._engine.connect() as connection:
                row = (await connection.execute(query)).one_or_none()
        except _UNAVAILABLE as error:
            raise ApprovalUnavailableError(type(error).__name__) from error
        return None if row is None else _approval(row)

    async def decide(
        self,
        approval_id: UUID,
        status: ApprovalStatus,
        by: str,
        at: datetime,
        note: str | None = None,
    ) -> Approval | None:
        statement = (
            update(approvals)
            .where(
                approvals.c.id == approval_id,
                approvals.c.status == ApprovalStatus.PENDING.value,  # first decision wins
            )
            .values(status=status.value, decided_at=at, decided_by=by, note=note)
            .returning(approvals)
        )
        try:
            async with self._engine.begin() as connection:
                row = (await connection.execute(statement)).one_or_none()
        except _UNAVAILABLE as error:
            raise ApprovalUnavailableError(type(error).__name__) from error
        return None if row is None else _approval(row)

    async def pending(self, now: datetime) -> Sequence[Approval]:
        query = (
            select(approvals)
            .where(
                approvals.c.status == ApprovalStatus.PENDING.value,
                approvals.c.expires_at > now,
            )
            .order_by(approvals.c.requested_at)
        )
        try:
            async with self._engine.connect() as connection:
                rows = (await connection.execute(query)).all()
        except _UNAVAILABLE as error:
            raise ApprovalUnavailableError(type(error).__name__) from error
        return [_approval(row) for row in rows]


def _columns(approval: Approval) -> dict[str, Any]:
    return {
        "id": approval.id,
        "tenant_id": approval.tenant_id,
        "tenant_name": approval.tenant_name,
        "tool": approval.tool,
        "arguments": json.dumps(approval.arguments, sort_keys=True),
        "reasons": json.dumps(list(approval.reasons)),
        "status": approval.status.value,
        "requested_at": approval.requested_at,
        "expires_at": approval.expires_at,
        "decided_at": approval.decided_at,
        "decided_by": approval.decided_by,
        "note": approval.note,
    }


def _approval(row: Row[Any]) -> Approval:
    return Approval(
        id=row.id,
        tenant_id=row.tenant_id,
        tenant_name=row.tenant_name,
        tool=row.tool,
        arguments=json.loads(row.arguments),
        reasons=tuple(json.loads(row.reasons)),
        status=ApprovalStatus(row.status),
        requested_at=row.requested_at,
        expires_at=row.expires_at,
        decided_at=row.decided_at,
        decided_by=row.decided_by,
        note=row.note,
    )
