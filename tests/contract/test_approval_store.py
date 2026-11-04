"""Behaviour every ApprovalStore must have: each test runs against the fake and real Postgres."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from llm_gateway.approvals.model import Approval, ApprovalStatus
from llm_gateway.approvals.ports import ApprovalStore, TooManyPendingError
from llm_gateway.config import Settings
from llm_gateway.main import build_engine
from llm_gateway.storage.approval_store import PostgresApprovalStore
from llm_gateway.storage.tenant_store import PostgresTenantStore
from llm_gateway.tenancy.model import Tenant
from tests.support.approvals import InMemoryApprovalStore
from tests.support.postgres import empty_tables

pytestmark = pytest.mark.anyio

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@dataclass
class Subject:
    store: ApprovalStore
    engine: AsyncEngine | None = None

    async def new_tenant(self) -> UUID:
        tenant = Tenant(id=uuid4(), name=f"t-{uuid4()}")
        if self.engine is not None:  # approvals reference a real tenant row
            await PostgresTenantStore(self.engine).add_tenant(tenant)
        return tenant.id


@pytest.fixture(params=["memory", "postgres"])
async def subject(request: pytest.FixtureRequest) -> AsyncIterator[Subject]:
    if request.param == "memory":
        yield Subject(InMemoryApprovalStore())
        return
    engine = build_engine(Settings(database_url=request.getfixturevalue("postgres_url")))
    try:
        await empty_tables(engine)
        yield Subject(PostgresApprovalStore(engine), engine)
    finally:
        await engine.dispose()


def an_approval(tenant_id: UUID, at: datetime = NOW, ttl_s: float = 120) -> Approval:
    return Approval(
        id=uuid4(),
        tenant_id=tenant_id,
        tenant_name="acme",
        tool="fs__move_file",
        arguments={"source": "/sandbox/a", "destination": "/sandbox/b", "n": [1, 2.5, None]},
        reasons=("fs-move-needs-approval",),
        status=ApprovalStatus.PENDING,
        requested_at=at,
        expires_at=at + timedelta(seconds=ttl_s),
    )


async def test_a_held_approval_reads_back_exactly(subject: Subject) -> None:
    approval = an_approval(await subject.new_tenant())

    await subject.store.hold(approval, max_pending=10, now=NOW)

    assert await subject.store.get(approval.id) == approval
    assert await subject.store.get(uuid4()) is None


async def test_a_decision_moves_it_out_of_pending_once(subject: Subject) -> None:
    approval = an_approval(await subject.new_tenant())
    await subject.store.hold(approval, max_pending=10, now=NOW)
    later = NOW + timedelta(seconds=5)

    approved = await subject.store.decide(approval.id, ApprovalStatus.APPROVED, "ops", later, "ok")
    second = await subject.store.decide(approval.id, ApprovalStatus.EXPIRED, "gateway", later)

    assert approved == replace(
        approval, status=ApprovalStatus.APPROVED, decided_at=later, decided_by="ops", note="ok"
    )
    assert second is None  # the first decision stands
    assert await subject.store.get(approval.id) == approved


async def test_two_deciding_at_once_exactly_one_wins(subject: Subject) -> None:
    approval = an_approval(await subject.new_tenant())
    await subject.store.hold(approval, max_pending=10, now=NOW)

    results = await asyncio.gather(
        *(
            subject.store.decide(approval.id, status, "x", NOW)
            for status in [ApprovalStatus.APPROVED, ApprovalStatus.EXPIRED] * 4
        )
    )

    assert len([r for r in results if r is not None]) == 1


async def test_pending_lists_what_still_waits_oldest_first(subject: Subject) -> None:
    tenant = await subject.new_tenant()
    newer = an_approval(tenant, at=NOW)
    older = an_approval(tenant, at=NOW - timedelta(seconds=30))
    expired = an_approval(tenant, at=NOW - timedelta(seconds=300))
    decided = an_approval(tenant, at=NOW - timedelta(seconds=10))
    for approval in (newer, older, expired, decided):
        await subject.store.hold(approval, max_pending=10, now=approval.requested_at)
    await subject.store.decide(decided.id, ApprovalStatus.REJECTED, "ops", NOW)

    assert [a.id for a in await subject.store.pending(NOW)] == [older.id, newer.id]


async def test_a_tenant_can_only_have_so_many_calls_waiting(subject: Subject) -> None:
    tenant, other = await subject.new_tenant(), await subject.new_tenant()
    for _ in range(2):
        await subject.store.hold(an_approval(tenant), max_pending=2, now=NOW)

    with pytest.raises(TooManyPendingError):
        await subject.store.hold(an_approval(tenant), max_pending=2, now=NOW)
    await subject.store.hold(an_approval(other), max_pending=2, now=NOW)  # its own limit


async def test_expired_and_decided_holds_dont_count_against_the_limit(subject: Subject) -> None:
    tenant = await subject.new_tenant()
    stale = an_approval(tenant, at=NOW - timedelta(seconds=300))  # its gateway died
    done = an_approval(tenant)
    await subject.store.hold(stale, max_pending=1, now=stale.requested_at)
    await subject.store.hold(done, max_pending=1, now=NOW)  # the stale one has expired
    await subject.store.decide(done.id, ApprovalStatus.APPROVED, "ops", NOW)

    await subject.store.hold(an_approval(tenant), max_pending=1, now=NOW)


async def test_concurrent_holds_cannot_all_slip_under_the_limit(subject: Subject) -> None:
    tenant = await subject.new_tenant()

    async def hold() -> bool:
        try:
            await subject.store.hold(an_approval(tenant), max_pending=3, now=NOW)
            return True
        except TooManyPendingError:
            return False

    held = await asyncio.gather(*(hold() for _ in range(10)))

    assert sum(held) == 3
