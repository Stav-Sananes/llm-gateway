"""Bearer-token authentication: `Authorization: Bearer gw_...`, for the API and /mcp."""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Header

from llm_gateway.tenancy.auth import Authenticator, InvalidApiKeyError
from llm_gateway.tenancy.model import Tenant

TenantDependency = Callable[..., Awaitable[Tenant]]


async def tenant_for_header(authenticator: Authenticator, authorization: str | None) -> Tenant:
    """Raises InvalidApiKeyError (or TenantStoreUnavailableError when Postgres is down)."""
    scheme, _, key = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not key.strip():
        raise InvalidApiKeyError("missing bearer API key")
    return await authenticator.authenticate(key.strip())


def bearer_tenant(authenticator: Authenticator) -> TenantDependency:
    """A FastAPI dependency that resolves the request's tenant or raises InvalidApiKeyError."""

    async def tenant(authorization: Annotated[str | None, Header()] = None) -> Tenant:
        return await tenant_for_header(authenticator, authorization)

    return tenant
