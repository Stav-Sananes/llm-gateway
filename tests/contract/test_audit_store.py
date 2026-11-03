"""Behaviour every AuditStore must have: each test runs against the fake and real Postgres."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from llm_gateway.audit.chain import EMPTY_CHAIN, AuditEvent, ChainHead
from llm_gateway.audit.log import AuditLog, Verification
from llm_gateway.audit.ports import AuditStore
from llm_gateway.config import Settings
from llm_gateway.main import build_engine
from llm_gateway.storage.audit_store import PostgresAuditStore
from llm_gateway.storage.tenant_store import PostgresTenantStore
from llm_gateway.tenancy.model import Tenant
from tests.support.in_memory_audit_store import InMemoryAuditStore
from tests.support.postgres import empty_tables

pytestmark = pytest.mark.anyio


@dataclass
class Subject:
    store: AuditStore
    engine: AsyncEngine | None = None

    async def new_tenant(self) -> UUID:
        tenant = Tenant(id=uuid4(), name=f"t-{uuid4()}")
        if self.engine is not None:  # audit rows reference a real tenant row
            await PostgresTenantStore(self.engine).add_tenant(tenant)
        return tenant.id


@pytest.fixture(params=["memory", "postgres"])
async def subject(request: pytest.FixtureRequest) -> AsyncIterator[Subject]:
    if request.param == "memory":
        yield Subject(InMemoryAuditStore())
        return
    engine = build_engine(Settings(database_url=request.getfixturevalue("postgres_url")))
    try:
        await empty_tables(engine)
        yield Subject(PostgresAuditStore(engine, batch_size=3), engine)
    finally:
        await engine.dispose()


def an_event(tenant_id: UUID, n: int = 0) -> AuditEvent:
    return AuditEvent(tenant_id, "tool.called", datetime.now(UTC), {"n": n})


async def test_appends_link_into_a_chain(subject: Subject) -> None:
    tenant = await subject.new_tenant()

    first = await subject.store.append(an_event(tenant, 1))
    second = await subject.store.append(an_event(tenant, 2))

    assert (first.seq, second.seq) == (1, 2)
    assert second.prev_hash == first.hash
    assert await subject.store.head(tenant) == ChainHead(2, second.hash)


async def test_a_tenant_without_events_has_an_empty_chain(subject: Subject) -> None:
    assert await subject.store.head(await subject.new_tenant()) == EMPTY_CHAIN


async def test_records_come_back_in_order_across_pages(subject: Subject) -> None:
    tenant = await subject.new_tenant()
    for n in range(7):  # more than two pages of 3 on Postgres
        await subject.store.append(an_event(tenant, n))

    seqs = [record.seq async for record in subject.store.records(tenant, up_to_seq=7)]

    assert seqs == [1, 2, 3, 4, 5, 6, 7]


async def test_stored_payload_is_the_exact_text_that_was_hashed(subject: Subject) -> None:
    tenant = await subject.new_tenant()
    appended = await subject.store.append(an_event(tenant, 1))

    [stored] = [record async for record in subject.store.records(tenant, up_to_seq=1)]

    assert stored == appended


async def test_concurrent_appends_for_one_tenant_form_one_unbroken_chain(
    subject: Subject,
) -> None:
    tenant = await subject.new_tenant()

    await asyncio.gather(*(subject.store.append(an_event(tenant, n)) for n in range(25)))

    result = await AuditLog(subject.store).verify(tenant)
    assert (result.ok, result.checked) == (True, 25)


async def test_tenants_have_separate_chains(subject: Subject) -> None:
    acme, globex = await subject.new_tenant(), await subject.new_tenant()
    for n in range(3):
        await subject.store.append(an_event(acme, n))
    globex_record = await subject.store.append(an_event(globex))

    assert globex_record.seq == 1
    assert [r.seq async for r in subject.store.records(globex, up_to_seq=10)] == [1]
    assert await subject.store.head(globex) == ChainHead(1, globex_record.hash)
    assert (await subject.store.head(acme)).seq == 3
    assert (await AuditLog(subject.store).verify(acme)).ok
    assert (await AuditLog(subject.store).verify(globex)).ok


async def test_records_stop_at_the_requested_seq(subject: Subject) -> None:
    tenant = await subject.new_tenant()
    for n in range(5):
        await subject.store.append(an_event(tenant, n))

    seqs = [record.seq async for record in subject.store.records(tenant, up_to_seq=3)]

    assert seqs == [1, 2, 3]


async def test_verify_is_clean_while_the_tenant_keeps_writing(subject: Subject) -> None:
    tenant = await subject.new_tenant()
    log = AuditLog(subject.store)
    await log.record(tenant, "tool.called", {})

    async def keep_writing() -> None:
        for n in range(40):
            await log.record(tenant, "tool.called", {"n": n})

    writer = asyncio.create_task(keep_writing())
    results: list[Verification] = []
    while not writer.done() and len(results) < 200:
        results.append(await log.verify(tenant))
        await asyncio.sleep(0)  # the in-memory store never suspends on its own
    await writer

    assert results
    assert all(result.ok for result in results), [r.problem for r in results if not r.ok]
