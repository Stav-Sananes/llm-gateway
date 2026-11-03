"""An attacker with write access to the audit tables: verify must notice every edit."""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from llm_gateway.audit.log import AuditLog
from llm_gateway.config import Settings
from llm_gateway.main import build_engine
from llm_gateway.storage.audit_store import PostgresAuditStore
from llm_gateway.storage.tenant_store import PostgresTenantStore
from llm_gateway.tenancy.model import Tenant
from tests.support.postgres import empty_tables

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
    ],
    ids=["edited", "deleted", "tail-cut", "hash-forged"],
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
