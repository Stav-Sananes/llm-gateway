"""`llm-gateway audit verify`: what an operator runs to check a tenant's audit trail."""

import asyncio
import re

import pytest

from llm_gateway.audit.log import AuditLog
from llm_gateway.cli import main
from llm_gateway.config import Settings
from llm_gateway.main import build_engine
from llm_gateway.storage.audit_store import PostgresAuditStore
from llm_gateway.storage.tenant_store import PostgresTenantStore
from tests.support.backing_services import new_api_key


def record_three_events(settings: Settings, tenant_name: str) -> None:
    async def record() -> None:
        engine = build_engine(settings)
        try:
            tenant = await PostgresTenantStore(engine).tenant_named(tenant_name)
            assert tenant is not None
            log = AuditLog(PostgresAuditStore(engine))
            for n in range(3):
                await log.record(tenant.id, "tool.called", {"n": n})
        finally:
            await engine.dispose()

    asyncio.run(record())


def head_hash(settings: Settings, tenant_name: str) -> str:
    async def read() -> str:
        engine = build_engine(settings)
        try:
            tenant = await PostgresTenantStore(engine).tenant_named(tenant_name)
            assert tenant is not None
            return (await PostgresAuditStore(engine).head(tenant.id)).hash
        finally:
            await engine.dispose()

    return asyncio.run(read())


def tamper(settings: Settings, sql: str) -> None:
    async def run() -> None:
        engine = build_engine(settings)
        try:
            async with engine.begin() as connection:
                await connection.exec_driver_sql(sql)
        finally:
            await engine.dispose()

    asyncio.run(run())


def verify(capsys: pytest.CaptureFixture[str], tenant: str) -> tuple[int, str, str]:
    exit_code = main(["audit", "verify", tenant])
    out, err = capsys.readouterr()
    return exit_code, out.strip(), err.strip()


def test_an_intact_chain_reports_ok_and_its_head(
    services: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    new_api_key(services, "acme")
    record_three_events(services, "acme")

    exit_code, out, _ = verify(capsys, "acme")

    assert exit_code == 0
    assert re.fullmatch(r"ok: 3 records, head [0-9a-f]{64}", out)
    assert out.endswith(head_hash(services, "acme"))


def test_an_edited_chain_is_reported_and_exits_2(
    services: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    new_api_key(services, "acme")
    record_three_events(services, "acme")
    tamper(services, "UPDATE audit_events SET payload = payload || ' ' WHERE seq = 2")

    exit_code, out, _ = verify(capsys, "acme")

    assert exit_code == 2
    assert out.startswith("TAMPERED: seq 2: hash does not match its content")


@pytest.mark.usefixtures("services")
def test_an_unknown_tenant_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code, _, err = verify(capsys, "nobody")

    assert exit_code == 1
    assert err == "error: no tenant named 'nobody'"
