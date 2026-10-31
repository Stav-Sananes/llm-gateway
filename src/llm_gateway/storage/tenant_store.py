"""Postgres-backed TenantStore. Same contract as the in-memory fake; see tests/contract."""

from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import Row
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from llm_gateway.storage.tables import TENANT_NAME_UNIQUE, api_keys, tenants
from llm_gateway.tenancy.model import StoredKey, Tenant
from llm_gateway.tenancy.ports import DuplicateTenantError, KeyNotFoundError


class PostgresTenantStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add_tenant(self, tenant: Tenant) -> None:
        try:
            async with self._engine.begin() as connection:
                await connection.execute(insert(tenants).values(id=tenant.id, name=tenant.name))
        except IntegrityError as error:
            if _violated_constraint(error) == TENANT_NAME_UNIQUE:
                raise DuplicateTenantError(f"tenant {tenant.name!r} already exists") from error
            raise

    async def tenant_named(self, name: str) -> Tenant | None:
        query = select(tenants.c.id, tenants.c.name).where(tenants.c.name == name)
        async with self._engine.connect() as connection:
            row = (await connection.execute(query)).one_or_none()
        return _tenant(row)

    async def add_key(self, key: StoredKey) -> None:
        async with self._engine.begin() as connection:
            await connection.execute(
                insert(api_keys).values(
                    prefix=key.prefix, key_hash=key.key_hash, tenant_id=key.tenant_id
                )
            )

    async def key_with_prefix(self, prefix: str) -> StoredKey | None:
        query = select(api_keys.c.tenant_id, api_keys.c.prefix, api_keys.c.key_hash).where(
            api_keys.c.prefix == prefix
        )
        async with self._engine.connect() as connection:
            row = (await connection.execute(query)).one_or_none()
        if row is None:
            return None
        return StoredKey(tenant_id=row.tenant_id, prefix=row.prefix, key_hash=row.key_hash)

    async def revoke_key(self, prefix: str) -> None:
        # coalesce keeps the first revocation time if the key is revoked twice.
        statement = (
            update(api_keys)
            .where(api_keys.c.prefix == prefix)
            .values(revoked_at=func.coalesce(api_keys.c.revoked_at, func.now()))
        )
        async with self._engine.begin() as connection:
            result = await connection.execute(statement)
        if result.rowcount == 0:
            raise KeyNotFoundError(f"no key with prefix {prefix!r}")

    async def tenant_for_key(self, key_hash: str) -> Tenant | None:
        query = (
            select(tenants.c.id, tenants.c.name)
            .join(api_keys, api_keys.c.tenant_id == tenants.c.id)
            .where(api_keys.c.key_hash == key_hash, api_keys.c.revoked_at.is_(None))
        )
        async with self._engine.connect() as connection:
            row = (await connection.execute(query)).one_or_none()
        return _tenant(row)


def _tenant(row: Row[Any, Any] | None) -> Tenant | None:
    return None if row is None else Tenant(id=row.id, name=row.name)


def _violated_constraint(error: IntegrityError) -> str | None:
    # asyncpg reports the constraint name on the driver exception that SQLAlchemy wraps.
    driver_error = getattr(error.orig, "__cause__", None)
    constraint: str | None = getattr(driver_error, "constraint_name", None)
    return constraint
