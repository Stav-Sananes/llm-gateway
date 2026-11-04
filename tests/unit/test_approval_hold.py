"""Parking a tool call until a human decides, and the admin's side of it."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import anyio
import pytest

from llm_gateway.approvals.desk import ApprovalDesk, ApprovalNotPendingError
from llm_gateway.approvals.hold import ApprovalHold, HoldSettings
from llm_gateway.approvals.model import GATEWAY, Approval, ApprovalStatus
from llm_gateway.approvals.ports import ApprovalUnavailableError, TooManyPendingError
from llm_gateway.policy.model import ToolCall
from tests.support.approvals import InMemoryApprovalStore

pytestmark = pytest.mark.anyio

MOVE = ToolCall(
    UUID(int=1), "acme", "fs", "move_file", {"source": "/sandbox/a", "destination": "/sandbox/b"}
)
REASONS = ("fs-move-needs-approval",)


class World:
    def __init__(self, timeout_s: float = 5.0, max_pending: int = 10) -> None:
        self.store = InMemoryApprovalStore()
        self.now = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
        settings = HoldSettings(
            timeout_s=timeout_s, poll_s=0.001, max_pending_per_tenant=max_pending
        )
        self.hold = ApprovalHold(self.store, settings, clock=lambda: self.now)
        self.desk = ApprovalDesk(self.store, clock=lambda: self.now)

    async def held(self) -> Approval:
        """The approval the hold has parked, once it has."""
        await self.store.held.wait()
        (approval,) = self.store.approvals.values()
        return approval


async def waiting_for(world: World, decide: str) -> Approval:
    """Wait on the hold while an admin decides ("approve" / "reject")."""
    result: list[Approval] = []

    async def wait() -> None:
        result.append(await world.hold.wait(MOVE, REASONS))

    with anyio.fail_after(2):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(wait)
            parked = await world.held()
            await getattr(world.desk, decide)(parked.id, "dana", "looks fine")
    return result[0]


async def test_a_held_call_waits_parked_with_exactly_what_would_run() -> None:
    world = World()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(world.hold.wait, MOVE, REASONS)
        parked = await world.held()
        tasks.cancel_scope.cancel()

    assert (parked.tool, parked.arguments, parked.reasons) == (
        "fs__move_file",
        MOVE.arguments,
        REASONS,
    )
    assert (parked.tenant_id, parked.tenant_name) == (MOVE.tenant_id, "acme")
    assert parked.expires_at == parked.requested_at + timedelta(seconds=5)


async def test_an_approval_lets_the_waiting_call_go_on() -> None:
    approval = await waiting_for(World(), "approve")

    assert approval.status is ApprovalStatus.APPROVED
    assert (approval.decided_by, approval.note) == ("dana", "looks fine")


async def test_a_rejection_reaches_the_waiting_call() -> None:
    assert (await waiting_for(World(), "reject")).status is ApprovalStatus.REJECTED


async def test_nobody_deciding_in_time_expires_the_hold() -> None:
    world = World(timeout_s=0.05)

    with anyio.fail_after(2):
        approval = await world.hold.wait(MOVE, REASONS)

    assert (approval.status, approval.decided_by) == (ApprovalStatus.EXPIRED, GATEWAY)
    assert world.store.approvals[approval.id].status is ApprovalStatus.EXPIRED


async def test_a_human_deciding_as_the_hold_times_out_wins() -> None:
    world = World(timeout_s=0.05)

    class ApprovesFirst(InMemoryApprovalStore):
        async def decide(
            self,
            approval_id: UUID,
            status: ApprovalStatus,
            by: str,
            at: datetime,
            note: str | None = None,
        ) -> Approval | None:
            if status is ApprovalStatus.EXPIRED:  # the human's update lands just before
                await super().decide(approval_id, ApprovalStatus.APPROVED, "dana", at)
            return await super().decide(approval_id, status, by, at, note)

    world.store = ApprovesFirst()
    world.hold = ApprovalHold(world.store, HoldSettings(timeout_s=0.05, poll_s=0.001))

    approval = await world.hold.wait(MOVE, REASONS)

    assert (approval.status, approval.decided_by) == (ApprovalStatus.APPROVED, "dana")


async def test_a_client_that_stops_waiting_abandons_the_hold() -> None:
    world = World()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(world.hold.wait, MOVE, REASONS)
        parked = await world.held()
        tasks.cancel_scope.cancel()  # the client disconnected

    assert world.store.approvals[parked.id].status is ApprovalStatus.ABANDONED
    with pytest.raises(ApprovalNotPendingError, match="abandoned"):
        await world.desk.approve(parked.id, "dana")  # approving now would run nothing


async def test_a_tenant_with_too_many_calls_waiting_is_refused_at_once() -> None:
    world = World(max_pending=1)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(world.hold.wait, MOVE, REASONS)
        await world.held()
        with pytest.raises(TooManyPendingError):
            await world.hold.wait(MOVE, REASONS)
        tasks.cancel_scope.cancel()


async def test_a_store_down_for_the_whole_hold_is_unavailable_never_approved() -> None:
    class DownOnceParked(InMemoryApprovalStore):
        async def hold(self, approval: Approval, max_pending: int, now: datetime) -> None:
            await super().hold(approval, max_pending, now)
            self.down = True

    hold = ApprovalHold(DownOnceParked(), HoldSettings(timeout_s=0.05, poll_s=0.001))

    with anyio.fail_after(2), pytest.raises(ApprovalUnavailableError):
        await hold.wait(MOVE, REASONS)


class LeftAfter:
    """A client that hangs up after `polls` polls."""

    def __init__(self, polls: int) -> None:
        self._polls = polls

    def has_left(self) -> bool:
        self._polls -= 1
        return self._polls < 0


async def test_a_client_that_hangs_up_abandons_its_hold() -> None:
    world = World()

    with anyio.fail_after(2):
        approval = await world.hold.wait(MOVE, REASONS, LeftAfter(polls=3))

    assert (approval.status, approval.decided_by) == (ApprovalStatus.ABANDONED, GATEWAY)
    assert world.store.approvals[approval.id].status is ApprovalStatus.ABANDONED


async def test_polls_that_fail_for_a_while_do_not_lose_an_approval() -> None:
    world = World()
    result: list[Approval] = []

    async def wait() -> None:
        result.append(await world.hold.wait(MOVE, REASONS))

    with anyio.fail_after(2):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(wait)
            parked = await world.held()
            await world.desk.approve(parked.id, "dana")
            world.store.down = True  # before the hold reads the approval, its polls fail
            await anyio.sleep(0.02)
            world.store.down = False

    assert result[0].status is ApprovalStatus.APPROVED


async def test_a_hold_that_can_never_be_read_expires_and_never_runs() -> None:
    class Unreadable(InMemoryApprovalStore):
        async def get(self, approval_id: UUID) -> Approval | None:
            raise ApprovalUnavailableError("pool timeout")

    store = Unreadable()
    hold = ApprovalHold(store, HoldSettings(timeout_s=0.05, poll_s=0.001))

    with anyio.fail_after(2):
        approval = await hold.wait(MOVE, REASONS)

    assert approval.status is ApprovalStatus.EXPIRED
    assert store.approvals[approval.id].status is ApprovalStatus.EXPIRED  # not left pending


async def test_a_decision_made_after_many_polls_still_reaches_the_waiter() -> None:
    class CountsPolls(InMemoryApprovalStore):
        polls = 0
        polled_three_times = anyio.Event()

        async def get(self, approval_id: UUID) -> Approval | None:
            self.polls += 1
            if self.polls >= 3:
                self.polled_three_times.set()
            return await super().get(approval_id)

    store = CountsPolls()
    hold = ApprovalHold(store, HoldSettings(timeout_s=5, poll_s=0.001))
    result: list[Approval] = []

    async def wait() -> None:
        result.append(await hold.wait(MOVE, REASONS))

    with anyio.fail_after(2):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(wait)
            await store.polled_three_times.wait()
            (parked,) = store.approvals.values()
            await ApprovalDesk(store).approve(parked.id, "dana")

    assert result[0].status is ApprovalStatus.APPROVED


async def test_a_hold_cancelled_while_being_parked_is_not_left_pending() -> None:
    class SlowToCommit(InMemoryApprovalStore):
        async def hold(self, approval: Approval, max_pending: int, now: datetime) -> None:
            await super().hold(approval, max_pending, now)  # committed...
            await anyio.sleep(60)  # ...but the caller is cancelled before it hears back

    store = SlowToCommit()
    hold = ApprovalHold(store, HoldSettings(timeout_s=5, poll_s=0.001))

    with anyio.move_on_after(0.05):
        await hold.wait(MOVE, REASONS)

    (approval,) = store.approvals.values()
    assert approval.status is ApprovalStatus.ABANDONED


# The admin's desk


async def test_the_desk_lists_what_waits() -> None:
    world = World()

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(world.hold.wait, MOVE, REASONS)
        parked = await world.held()
        assert [a.id for a in await world.desk.waiting()] == [parked.id]
        tasks.cancel_scope.cancel()


async def test_an_approval_can_only_be_decided_once() -> None:
    world = World()
    approval = await waiting_for(world, "reject")

    with pytest.raises(ApprovalNotPendingError, match="rejected"):
        await world.desk.approve(approval.id, "dana")


async def test_a_hold_past_its_expiry_cant_be_approved_even_if_still_pending() -> None:
    # Its gateway died before closing it: nobody waits for the decision any more.
    world = World()
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(world.hold.wait, MOVE, REASONS)
        parked = await world.held()
        tasks.cancel_scope.cancel()
    world.store.approvals[parked.id] = parked  # as if never closed
    world.now = parked.expires_at  # not a moment past it: already too late

    with pytest.raises(ApprovalNotPendingError, match="expired"):
        await world.desk.approve(parked.id, "dana")
    assert world.store.approvals[parked.id].status is ApprovalStatus.EXPIRED


@pytest.mark.parametrize("by", ["", "  ", GATEWAY, "Gateway", " gateway "])
async def test_a_decision_needs_a_persons_name(by: str) -> None:
    world = World()

    with pytest.raises(ValueError, match="name"):
        await world.desk.approve(UUID(int=9), by)
