"""The approval hold: park a tool call in the store and wait for a human's decision.

    hold (pending) ─► poll ─► approved / rejected by a human ─► the caller acts on it
                         └─► timeout ─► expired       (the gateway closes it)
                         └─► client gone / error ─► abandoned

Every move out of `pending` is one conditional update, so when a human and the timeout
decide at the same moment, exactly one wins and both sides see the same outcome.
"""

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import anyio

from llm_gateway.approvals.model import GATEWAY, Approval, ApprovalStatus
from llm_gateway.approvals.ports import ApprovalStore, ApprovalUnavailableError
from llm_gateway.policy.model import ToolCall

logger = logging.getLogger(__name__)

_CLOSE_TIMEOUT_S = 5.0  # closing a hold on the way out, even when cancelled


@dataclass(frozen=True)
class HoldSettings:
    timeout_s: float = 120.0  # how long the client waits for a human
    poll_s: float = 0.5
    max_pending_per_tenant: int = 10  # each held call keeps a client request open


DEFAULT_HOLD_SETTINGS = HoldSettings()


def utc_now() -> datetime:
    return datetime.now(UTC)


class ApprovalHold:
    def __init__(
        self,
        store: ApprovalStore,
        settings: HoldSettings = DEFAULT_HOLD_SETTINGS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._store = store
        self._settings = settings
        self._clock = clock

    @property
    def timeout_s(self) -> float:
        return self._settings.timeout_s

    async def wait(self, call: ToolCall, reasons: Sequence[str]) -> Approval:
        """Park the call and wait. Returns it approved, rejected or expired.

        Raises TooManyPendingError, or ApprovalUnavailableError when the store fails:
        either way the call must not run."""
        approval = self._new(call, reasons)
        await self._store.hold(approval, self._settings.max_pending_per_tenant, self._clock())
        logger.info("tool call %s held for approval %s", call.qualified_name, approval.id)
        try:
            with anyio.move_on_after(self._settings.timeout_s):
                while True:
                    await anyio.sleep(self._settings.poll_s)
                    current = await self._store.get(approval.id)
                    if current is None:
                        raise ApprovalUnavailableError(f"approval {approval.id} disappeared")
                    if current.status is not ApprovalStatus.PENDING:
                        return current
            return await self._close(approval, ApprovalStatus.EXPIRED)
        except BaseException:
            # Cancelled (the client left) or failed: nobody is waiting for a decision now.
            with anyio.move_on_after(_CLOSE_TIMEOUT_S, shield=True):
                await self._close_quietly(approval)
            raise

    def _new(self, call: ToolCall, reasons: Sequence[str]) -> Approval:
        now = self._clock()
        return Approval(
            id=uuid.uuid4(),
            tenant_id=call.tenant_id,
            tenant_name=call.tenant_name,
            tool=call.qualified_name,
            arguments=call.arguments,
            reasons=tuple(reasons),
            status=ApprovalStatus.PENDING,
            requested_at=now,
            expires_at=now + timedelta(seconds=self._settings.timeout_s),
        )

    async def _close(self, approval: Approval, status: ApprovalStatus) -> Approval:
        closed = await self._store.decide(approval.id, status, GATEWAY, self._clock())
        if closed is not None:
            return closed
        current = await self._store.get(approval.id)  # a human decided first: that stands
        if current is None:
            raise ApprovalUnavailableError(f"approval {approval.id} disappeared")
        return current

    async def _close_quietly(self, approval: Approval) -> None:
        try:
            await self._store.decide(approval.id, ApprovalStatus.ABANDONED, GATEWAY, self._clock())
        except ApprovalUnavailableError as error:
            logger.warning("approval %s left pending: %s", approval.id, error)
