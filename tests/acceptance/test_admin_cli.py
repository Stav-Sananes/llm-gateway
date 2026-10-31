"""The admin CLI against a real, migrated Postgres: what an operator actually types."""

import asyncio

import pytest

from llm_gateway.cli import main
from llm_gateway.config import Settings
from llm_gateway.main import build_engine
from llm_gateway.storage.tenant_store import PostgresTenantStore
from llm_gateway.tenancy.keys import hash_key
from llm_gateway.tenancy.model import Tenant
from tests.support.postgres import empty_tables


@pytest.fixture(autouse=True)
def empty_database(postgres_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GATEWAY_DATABASE_URL", postgres_url)
    asyncio.run(_empty(postgres_url))


def tenant_for(postgres_url: str, plaintext_key: str) -> Tenant | None:
    return asyncio.run(_tenant_for_key(postgres_url, hash_key(plaintext_key)))


async def _empty(postgres_url: str) -> None:
    engine = build_engine(Settings(database_url=postgres_url))
    try:
        await empty_tables(engine)
    finally:
        await engine.dispose()


async def _tenant_for_key(postgres_url: str, key_hash: str) -> Tenant | None:
    engine = build_engine(Settings(database_url=postgres_url))
    try:
        return await PostgresTenantStore(engine).tenant_for_key(key_hash)
    finally:
        await engine.dispose()


def admin(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    exit_code = main(["admin", *args])
    out, err = capsys.readouterr()
    return exit_code, out.strip(), err.strip()


def test_issued_key_authenticates_as_its_tenant(
    postgres_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert admin(capsys, "create-tenant", "acme")[0] == 0

    exit_code, key, warning = admin(capsys, "create-key", "acme")

    assert exit_code == 0
    assert key.startswith("gw_")
    assert "not be shown again" in warning
    tenant = tenant_for(postgres_url, key)
    assert tenant is not None
    assert tenant.name == "acme"


def test_revoked_key_no_longer_authenticates(
    postgres_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    admin(capsys, "create-tenant", "acme")
    key = admin(capsys, "create-key", "acme")[1]
    prefix = key.split("_", 2)[1]

    exit_code, out, _ = admin(capsys, "revoke-key", prefix)

    assert exit_code == 0
    assert out == f"revoked key {prefix}"
    assert tenant_for(postgres_url, key) is None


@pytest.mark.parametrize(
    ("command", "expected_error"),
    [
        (["create-key", "nobody"], "no tenant named 'nobody'"),
        (["revoke-key", "deadbeef"], "no key with prefix 'deadbeef'"),
    ],
)
def test_unknown_names_fail_with_a_message_and_print_no_key(
    capsys: pytest.CaptureFixture[str], command: list[str], expected_error: str
) -> None:
    exit_code, out, err = admin(capsys, *command)

    assert exit_code == 1
    assert out == ""
    assert err == f"error: {expected_error}"


def test_duplicate_tenant_fails_with_a_message(capsys: pytest.CaptureFixture[str]) -> None:
    admin(capsys, "create-tenant", "acme")

    exit_code, _, err = admin(capsys, "create-tenant", "acme")

    assert exit_code == 1
    assert err == "error: tenant 'acme' already exists"


def test_migrate_is_safe_to_run_again(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrate"]) == 0
    assert capsys.readouterr().out.strip() == "database is up to date"
