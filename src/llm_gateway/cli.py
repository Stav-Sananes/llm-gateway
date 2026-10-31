"""Command line: serve the gateway, migrate the database, and administer tenants and keys."""

import argparse
import asyncio
import sys
from collections.abc import Sequence

from llm_gateway.config import Settings
from llm_gateway.main import build_engine, build_redis, run
from llm_gateway.storage.key_cache import RedisKeyCache
from llm_gateway.storage.migrate import upgrade_to_head
from llm_gateway.storage.tenant_store import PostgresTenantStore
from llm_gateway.tenancy.admin import KeyCacheNotClearedError, TenantAdmin
from llm_gateway.tenancy.ports import DuplicateTenantError, KeyNotFoundError, TenantNotFoundError

ADMIN_ERRORS = (
    DuplicateTenantError,
    KeyCacheNotClearedError,
    KeyNotFoundError,
    TenantNotFoundError,
)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    settings = Settings()
    if args.command == "serve":
        run(settings)
    elif args.command == "migrate":
        upgrade_to_head(settings.database_url)
        print("database is up to date")
    else:
        return asyncio.run(_admin(args, settings))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-gateway")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the gateway HTTP server")
    commands.add_parser("migrate", help="bring the database schema up to date")

    admin = commands.add_parser("admin", help="manage tenants and API keys")
    actions = admin.add_subparsers(dest="action", required=True)
    actions.add_parser("create-tenant", help="add a tenant").add_argument("name")
    actions.add_parser("create-key", help="issue an API key (printed once)").add_argument("tenant")
    actions.add_parser("revoke-key", help="revoke an API key by its prefix").add_argument("prefix")
    return parser


async def _admin(args: argparse.Namespace, settings: Settings) -> int:
    engine = build_engine(settings)
    redis = build_redis(settings)
    cache = RedisKeyCache(redis, settings.key_cache_ttl_s)
    admin = TenantAdmin(PostgresTenantStore(engine), cache)
    try:
        await _run_admin_action(admin, args)
    except ADMIN_ERRORS as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    finally:
        await redis.aclose()
        await engine.dispose()
    return 0


async def _run_admin_action(admin: TenantAdmin, args: argparse.Namespace) -> None:
    if args.action == "create-tenant":
        tenant = await admin.create_tenant(args.name)
        print(f"created tenant {tenant.name} ({tenant.id})")
    elif args.action == "create-key":
        issued = await admin.create_key(args.tenant)
        # The key alone on stdout, so scripts can capture it; the warning goes to stderr.
        print(issued.plaintext)
        print(f"key {issued.prefix} issued; it will not be shown again", file=sys.stderr)
    else:
        await admin.revoke_key(args.prefix)
        print(f"revoked key {args.prefix}")
