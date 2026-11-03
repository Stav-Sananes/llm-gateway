"""Maps domain errors and validation failures to OpenAI-style error responses."""

import logging
import math

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from llm_gateway.chat.ports import ProviderRejectedError, ProviderUnavailableError
from llm_gateway.http.presenter import error_payload
from llm_gateway.limits.ports import (
    BudgetExhaustedError,
    BudgetUnavailableError,
    RateLimitedError,
)
from llm_gateway.tenancy.auth import InvalidApiKeyError
from llm_gateway.tenancy.ports import TenantStoreUnavailableError

logger = logging.getLogger(__name__)


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(InvalidApiKeyError, _invalid_api_key)
    app.add_exception_handler(TenantStoreUnavailableError, _tenant_store_unavailable)
    app.add_exception_handler(RateLimitedError, _rate_limited)
    app.add_exception_handler(BudgetExhaustedError, _budget_exhausted)
    app.add_exception_handler(BudgetUnavailableError, _budget_unavailable)
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
    # Only the cause's type: the message holds the SQL parameters, which include the key hash.
    logger.error("tenant store unavailable (%s)", type(error.__cause__).__name__)
    return JSONResponse(
        error_payload("Authentication is temporarily unavailable.", "service_unavailable"),
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
    )


async def _rate_limited(_: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, RateLimitedError):
        raise error
    return JSONResponse(
        error_payload(
            "Rate limit reached. Retry after the time in the Retry-After header.",
            "requests",
            "rate_limit_exceeded",
        ),
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        headers={"Retry-After": str(math.ceil(error.retry_after_s))},
    )


async def _budget_exhausted(_: Request, error: Exception) -> JSONResponse:
    return JSONResponse(
        error_payload(
            "Token budget for this period is used up.", "insufficient_quota", "insufficient_quota"
        ),
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
    )


async def _budget_unavailable(_: Request, error: Exception) -> JSONResponse:
    # Fail closed: without the budget store, spend can't be capped.
    logger.error("token budget unavailable (%s)", type(error.__cause__).__name__)
    return JSONResponse(
        error_payload("Usage limits are temporarily unavailable.", "service_unavailable"),
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
