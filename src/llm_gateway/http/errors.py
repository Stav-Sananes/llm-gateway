"""Maps domain errors and validation failures to OpenAI-style error responses."""

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from llm_gateway.chat.ports import ProviderRejectedError, ProviderUnavailableError
from llm_gateway.http.presenter import error_payload
from llm_gateway.tenancy.auth import InvalidApiKeyError
from llm_gateway.tenancy.ports import TenantStoreUnavailableError


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(InvalidApiKeyError, _invalid_api_key)
    app.add_exception_handler(TenantStoreUnavailableError, _tenant_store_unavailable)
    app.add_exception_handler(ProviderUnavailableError, _provider_unavailable)
    app.add_exception_handler(ProviderRejectedError, _provider_rejected)
    app.add_exception_handler(RequestValidationError, _invalid_request)


async def _invalid_api_key(_: Request, error: Exception) -> JSONResponse:
    # One message for every case: don't tell a caller which part of its key was wrong.
    return JSONResponse(
        error_payload("Invalid API key.", "invalid_request_error", "invalid_api_key"),
        status_code=status.HTTP_401_UNAUTHORIZED,
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _tenant_store_unavailable(_: Request, error: Exception) -> JSONResponse:
    return JSONResponse(
        error_payload("Authentication is temporarily unavailable.", "service_unavailable"),
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


async def _provider_unavailable(_: Request, error: Exception) -> JSONResponse:
    return JSONResponse(
        error_payload(str(error), "upstream_error"), status_code=status.HTTP_502_BAD_GATEWAY
    )


async def _provider_rejected(_: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, ProviderRejectedError):
        raise error
    return JSONResponse(
        error_payload(error.message, "invalid_request_error"), status_code=error.status_code
    )


async def _invalid_request(_: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, RequestValidationError):
        raise error
    return JSONResponse(
        error_payload(_describe(error), "invalid_request_error"),
        status_code=status.HTTP_400_BAD_REQUEST,
    )


def _describe(error: RequestValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
        for issue in error.errors()
    )
