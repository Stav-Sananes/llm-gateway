"""The admin CLI against real Postgres and Redis: what an operator actually types."""

import asyncio

import pytest

from llm_gateway.cli import main
from llm_gateway.config import Settings
from llm_gateway.tenancy.auth import Authenticator, InvalidApiKeyError
from llm_gateway.tenancy.model import Tenant
from tests.support.backing_services import real_store_and_cache


def tenant_for(settings: Settings, plaintext_key: str) -> Tenant | None:
    """Authenticate the way the gateway does: Redis first, then Postgres."""
    return asyncio.run(_authenticate(settings, plaintext_key))


async def _authenticate(settings: Settings, plaintext_key: str) -> Tenant | None:
    async with real_store_and_cache(settings) as (store, cache):
        try:
            return await Authenticator(store, cache).authenticate(plaintext_key)
        except InvalidApiKeyError:
            return None


def admin(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    exit_code = main(["admin", *args])
    out, err = capsys.readouterr()
    return exit_code, out.strip(), err.strip()


def test_issued_key_authenticates_as_its_tenant(
    services: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    assert admin(capsys, "create-tenant", "acme")[0] == 0

    exit_code, key, warning = admin(capsys, "create-key", "acme")

    assert exit_code == 0
    assert key.startswith("gw_")
    assert "not be shown again" in warning
    tenant = tenant_for(services, key)
    assert tenant is not None
    assert tenant.name == "acme"


def test_revoked_key_no_longer_authenticates_even_when_cached(
    services: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    admin(capsys, "create-tenant", "acme")
    key = admin(capsys, "create-key", "acme")[1]
    prefix = key.split("_", 2)[1]
    assert tenant_for(services, key) is not None  # now cached in Redis

    exit_code, out, _ = admin(capsys, "revoke-key", prefix)

    assert exit_code == 0
    assert out == f"revoked key {prefix}"
    assert tenant_for(services, key) is None


@pytest.mark.parametrize(
    ("command", "expected_error"),
    [
        (["create-key", "nobody"], "no tenant named 'nobody'"),
        (["revoke-key", "deadbeef"], "no key with prefix 'deadbeef'"),
    ],
)
@pytest.mark.usefixtures("services")
def test_unknown_names_fail_with_a_message_and_print_no_key(
    capsys: pytest.CaptureFixture[str], command: list[str], expected_error: str
) -> None:
    exit_code, out, err = admin(capsys, *command)

    assert exit_code == 1
    assert out == ""
    assert err == f"error: {expected_error}"


@pytest.mark.usefixtures("services")
def test_duplicate_tenant_fails_with_a_message(capsys: pytest.CaptureFixture[str]) -> None:
    admin(capsys, "create-tenant", "acme")

    exit_code, _, err = admin(capsys, "create-tenant", "acme")

    assert exit_code == 1
    assert err == "error: tenant 'acme' already exists"


@pytest.mark.usefixtures("services")
def test_migrate_is_safe_to_run_again(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrate"]) == 0
    assert capsys.readouterr().out.strip() == "database is up to date"
