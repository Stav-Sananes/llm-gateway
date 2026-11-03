"""An attacker with write access to the audit tables: verify must notice every edit."""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from llm_gateway.audit.chain import link_hash
from llm_gateway.audit.log import AuditLog
from llm_gateway.audit.ports import AuditUnavailableError
from llm_gateway.config import Settings
from llm_gateway.main import build_engine
from llm_gateway.storage.audit_store import PostgresAuditStore
from llm_gateway.storage.tenant_store import PostgresTenantStore
from llm_gateway.tenancy.model import Tenant
from tests.support.postgres import empty_tables
from tests.support.servers import unused_local_url

pytestmark = pytest.mark.anyio


@pytest.fixture
async def engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    engine = build_engine(Settings(database_url=postgres_url))
    await empty_tables(engine)
    yield engine
    await engine.dispose()


async def a_chain_of_five(engine: AsyncEngine) -> tuple[AuditLog, UUID]:
    tenant = Tenant(id=uuid4(), name="acme")
    await PostgresTenantStore(engine).add_tenant(tenant)
    log = AuditLog(PostgresAuditStore(engine))
    for n in range(5):
        await log.record(tenant.id, "tool.called", {"tool": "sql__query", "n": n})
    return log, tenant.id


async def tamper(engine: AsyncEngine, sql: str, tenant_id: UUID) -> None:
    async with engine.begin() as connection:
        await connection.exec_driver_sql(sql, (tenant_id,))


@pytest.mark.parametrize(
    ("sql", "problem"),
    [
        (
            "UPDATE audit_events SET payload = replace(payload, 'sql__query', 'fs__read')"
            " WHERE tenant_id = $1 AND seq = 3",
            "seq 3: hash does not match its content",
        ),
        (
            "DELETE FROM audit_events WHERE tenant_id = $1 AND seq = 2",
            "seq 2: missing; next stored record is seq 3",
        ),
        (
            "DELETE FROM audit_events WHERE tenant_id = $1 AND seq >= 4",
            "chain ends at seq 3 but its head says seq 5",
        ),
        (
            "UPDATE audit_events SET hash = repeat('a', 64) WHERE tenant_id = $1 AND seq = 2",
            "seq 2: hash does not match its content",
        ),
        (
            "UPDATE audit_heads SET hash = repeat('b', 64) WHERE tenant_id = $1",
            "head hash does not match the hash of seq 5",
        ),
    ],
    ids=["edited", "deleted", "tail-cut", "hash-forged", "head-forged"],
)
async def test_verify_finds_tampering(engine: AsyncEngine, sql: str, problem: str) -> None:
    log, tenant_id = await a_chain_of_five(engine)
    await tamper(engine, sql, tenant_id)

    result = await log.verify(tenant_id)

    assert not result.ok
    assert result.problem == problem


async def test_an_untouched_chain_verifies(engine: AsyncEngine) -> None:
    log, tenant_id = await a_chain_of_five(engine)

    assert (await log.verify(tenant_id)).ok


async def test_another_tenants_chain_copied_over_is_found(engine: AsyncEngine) -> None:
    _, acme = await a_chain_of_five(engine)
    globex = Tenant(id=uuid4(), name="globex")
    await PostgresTenantStore(engine).add_tenant(globex)
    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            "INSERT INTO audit_events (tenant_id, seq, prev_hash, hash, payload)"
            " SELECT $1, seq, prev_hash, hash, payload FROM audit_events WHERE tenant_id = $2",
            (globex.id, acme),
        )
        await connection.exec_driver_sql(
            "INSERT INTO audit_heads SELECT $1, seq, hash FROM audit_heads WHERE tenant_id = $2",
            (globex.id, acme),
        )

    result = await AuditLog(PostgresAuditStore(engine)).verify(globex.id)

    assert result.problem == "seq 1: belongs to another tenant"


async def test_rows_verify_would_never_read_cannot_be_stored(engine: AsyncEngine) -> None:
    _, tenant_id = await a_chain_of_five(engine)

    with pytest.raises(IntegrityError, match="ck_audit_events_seq_positive"):
        await tamper(
            engine,
            "INSERT INTO audit_events (tenant_id, seq, prev_hash, hash, payload)"
            " VALUES ($1, 0, '', '', '{}')",
            tenant_id,
        )


async def test_renumbered_records_after_a_deletion_are_found(engine: AsyncEngine) -> None:
    log, tenant_id = await a_chain_of_five(engine)
    # Delete seq 3, slide 4 and 5 down, and point the head at the new end: every row
    # still hashes correctly on its own, only the links between them give it away.
    async with engine.begin() as connection:
        for sql in (
            "DELETE FROM audit_events WHERE tenant_id = $1 AND seq = 3",
            "UPDATE audit_events SET seq = seq - 1 WHERE tenant_id = $1 AND seq = 4",
            "UPDATE audit_events SET seq = seq - 1 WHERE tenant_id = $1 AND seq = 5",
            "UPDATE audit_heads SET seq = 4 WHERE tenant_id = $1",
        ):
            await connection.exec_driver_sql(sql, (tenant_id,))

    result = await log.verify(tenant_id)

    assert result.problem == "seq 3: does not link to the record before it"


async def test_a_consistently_rewritten_last_record_is_found_by_the_head(
    engine: AsyncEngine,
) -> None:
    log, tenant_id = await a_chain_of_five(engine)
    async with engine.connect() as connection:
        prev_hash = (
            await connection.exec_driver_sql(
                "SELECT prev_hash FROM audit_events WHERE tenant_id = $1 AND seq = 5", (tenant_id,)
            )
        ).scalar_one()
    forged = f'{{"action":"nothing happened","tenant_id":"{tenant_id}"}}'
    async with engine.begin() as connection:
        await connection.exec_driver_sql(
            "UPDATE audit_events SET payload = $2, hash = $3 WHERE tenant_id = $1 AND seq = 5",
            (tenant_id, forged, link_hash(prev_hash, forged)),
        )

    result = await log.verify(tenant_id)

    assert result.problem == "head hash does not match the hash of seq 5"


# Postgres happens to read this through the primary-key index, so the order would hold even
# without ORDER BY; the query's ORDER BY is what guarantees it as tables and plans change.
async def test_records_are_read_in_seq_order_whatever_the_storage_order(
    engine: AsyncEngine,
) -> None:
    _, tenant_id = await a_chain_of_five(engine)
    async with engine.begin() as connection:  # rewrite rows so physical order is reversed
        await connection.exec_driver_sql(
            "CREATE TEMP TABLE copy ON COMMIT DROP AS"
            " SELECT * FROM audit_events WHERE tenant_id = $1",
            (tenant_id,),
        )
        await connection.exec_driver_sql(
            "DELETE FROM audit_events WHERE tenant_id = $1", (tenant_id,)
        )
        await connection.exec_driver_sql(
            "INSERT INTO audit_events SELECT * FROM copy ORDER BY seq DESC"
        )
    store = PostgresAuditStore(engine, batch_size=2)

    seqs = [record.seq async for record in store.records(tenant_id, up_to_seq=5)]

    assert seqs == [1, 2, 3, 4, 5]
    assert (await AuditLog(store).verify(tenant_id)).ok


async def test_appending_with_the_database_down_refuses_the_action() -> None:
    port = unused_local_url().rsplit(":", 1)[1]
    engine = build_engine(Settings(database_url=f"postgresql+asyncpg://u:p@127.0.0.1:{port}/db"))
    try:
        with pytest.raises(AuditUnavailableError):
            await AuditLog(PostgresAuditStore(engine)).record(uuid4(), "tool.called", {})
    finally:
        await engine.dispose()
