"""Bearer-token authentication for the API: `Authorization: Bearer gw_...`."""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Header

from llm_gateway.tenancy.auth import Authenticator, InvalidApiKeyError
from llm_gateway.tenancy.model import Tenant

TenantDependency = Callable[..., Awaitable[Tenant]]


def bearer_tenant(authenticator: Authenticator) -> TenantDependency:
    """A FastAPI dependency that resolves the request's tenant or raises InvalidApiKeyError."""

    async def tenant(authorization: Annotated[str | None, Header()] = None) -> Tenant:
        scheme, _, key = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not key.strip():
            raise InvalidApiKeyError("missing bearer API key")
        return await authenticator.authenticate(key.strip())

    return tenant
