"""The approval hold: park a tool call in the store and wait for a human's decision.

    hold (pending) ─► poll ─► approved / rejected by a human ─► the caller acts on it
                         └─► timeout ─► expired       (the gateway closes it)
                         └─► client gone / cancelled ─► abandoned

A poll that fails (the store briefly unreachable) is retried until the deadline: a human
may already have approved, and one bad read must not lose that.

Every move out of `pending` is one conditional update, so when a human and the timeout
decide at the same moment, exactly one wins and both sides see the same outcome.
"""

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

import anyio

from llm_gateway.approvals.model import GATEWAY, Approval, ApprovalStatus
from llm_gateway.approvals.ports import ApprovalStore, ApprovalUnavailableError
from llm_gateway.policy.model import ToolCall

logger = logging.getLogger(__name__)

_CLOSE_TIMEOUT_S = 5.0  # closing a hold on the way out, even when cancelled


class Waiter(Protocol):
    """Whoever waits for the decision: the client of the held call."""

    def has_left(self) -> bool: ...


class _StaysWaiting:
    def has_left(self) -> bool:
        return False


STAYS_WAITING = _StaysWaiting()


@dataclass(frozen=True)
class HoldSettings:
    # How long the client waits for a human. Keep it below the MCP client's own request
    # timeout (60 s in the TypeScript SDK), or the client gives up first.
    timeout_s: float = 50.0
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

    async def wait(
        self, call: ToolCall, reasons: Sequence[str], waiter: Waiter = STAYS_WAITING
    ) -> Approval:
        """Park the call and wait. Returns it approved, rejected, expired, or abandoned
        (its client left).

        Raises TooManyPendingError, or ApprovalUnavailableError when the store can't park
        or close it: either way the call must not run."""
        approval = self._new(call, reasons)
        try:
            await self._store.hold(approval, self._settings.max_pending_per_tenant, self._clock())
        except anyio.get_cancelled_exc_class():
            await self._abandon(approval)  # the insert may have committed before the cancel
            raise
        logger.info("tool call %s held for approval %s", call.qualified_name, approval.id)
        try:
            with anyio.move_on_after(self._settings.timeout_s):
                while True:
                    await anyio.sleep(self._settings.poll_s)
                    if waiter.has_left():
                        return await self._close(approval, ApprovalStatus.ABANDONED)
                    if (current := await self._poll(approval)) is not None:
                        return current
            return await self._close(approval, ApprovalStatus.EXPIRED)
        except anyio.get_cancelled_exc_class():
            await self._abandon(approval)  # shutting down: nobody will act on a decision
            raise

    async def _poll(self, approval: Approval) -> Approval | None:
        """The approval once decided; None while pending or unreadable for now."""
        try:
            current = await self._store.get(approval.id)
        except ApprovalUnavailableError as error:
            logger.warning("approval %s not readable, still waiting: %s", approval.id, error)
            return None
        if current is None:
            raise ApprovalUnavailableError(f"approval {approval.id} disappeared")
        return None if current.status is ApprovalStatus.PENDING else current

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

    async def _abandon(self, approval: Approval) -> None:
        """Close the hold on the way out, even inside a cancellation. Never raises."""
        with anyio.move_on_after(_CLOSE_TIMEOUT_S, shield=True):
            try:
                closed = await self._store.decide(
                    approval.id, ApprovalStatus.ABANDONED, GATEWAY, self._clock()
                )
                current = closed or await self._store.get(approval.id)
            except ApprovalUnavailableError as error:
                logger.warning("approval %s left pending: %s", approval.id, error)
                return
            if closed is None and current is not None and current.decided_by != GATEWAY:
                logger.warning(
                    "approval %s was %s by %s, but its call was abandoned and won't run",
                    approval.id, current.status, current.decided_by,
                )  # fmt: skip
