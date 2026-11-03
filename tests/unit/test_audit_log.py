import asyncio
from uuid import UUID

import pytest

from llm_gateway.audit.log import AuditLog
from llm_gateway.audit.redaction import REDACTED
from tests.support.in_memory_audit_store import InMemoryAuditStore

pytestmark = pytest.mark.anyio

ACME, GLOBEX = UUID(int=1), UUID(int=2)


async def a_chain(store: InMemoryAuditStore, length: int) -> AuditLog:
    log = AuditLog(store)
    for n in range(length):
        await log.record(ACME, "tool.called", {"n": n})
    return log


async def test_an_untouched_chain_verifies() -> None:
    log = await a_chain(InMemoryAuditStore(), 5)

    result = await log.verify(ACME)

    assert (result.ok, result.checked, result.problem) == (True, 5, None)


async def test_an_empty_chain_verifies() -> None:
    assert (await AuditLog(InMemoryAuditStore()).verify(ACME)).ok


async def test_an_edited_record_is_found() -> None:
    store = InMemoryAuditStore()
    log = await a_chain(store, 5)
    store.edit_payload(ACME, 3, '{"action":"nothing to see"}')

    result = await log.verify(ACME)

    assert not result.ok
    assert result.problem == "seq 3: hash does not match its content"


async def test_a_deleted_record_is_found() -> None:
    store = InMemoryAuditStore()
    log = await a_chain(store, 5)
    store.delete(ACME, 2)

    result = await log.verify(ACME)

    assert not result.ok
    assert result.problem == "seq 2: missing; next stored record is seq 3"


async def test_a_cut_off_tail_is_found() -> None:
    store = InMemoryAuditStore()
    log = await a_chain(store, 5)
    store.delete(ACME, 5)

    result = await log.verify(ACME)

    assert not result.ok
    assert result.problem == "chain ends at seq 4 but its head says seq 5"


async def test_secrets_are_redacted_before_they_are_hashed() -> None:
    store = InMemoryAuditStore()

    record = await AuditLog(store).record(ACME, "tool.called", {"password": "hunter2"})

    assert "hunter2" not in record.payload
    assert REDACTED in record.payload


async def test_tenants_have_separate_chains() -> None:
    store = InMemoryAuditStore()
    log = AuditLog(store)

    first_acme = await log.record(ACME, "a", {})
    first_globex = await log.record(GLOBEX, "a", {})

    assert (first_acme.seq, first_globex.seq) == (1, 1)


async def test_concurrent_records_form_one_unbroken_chain() -> None:
    store = InMemoryAuditStore()
    log = AuditLog(store)

    await asyncio.gather(*(log.record(ACME, "tool.called", {"n": n}) for n in range(30)))

    assert (await log.verify(ACME)).checked == 30
