"""The /mcp endpoint: one MCP server (Streamable HTTP) in front of every upstream server.

Stateless: each request is self-contained, so any replica can serve any request. A small
ASGI layer authenticates the bearer key before the MCP SDK sees the request, and leaves the
tenant in the request state for the tool handlers.
"""

from collections.abc import Mapping
from typing import Any

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from llm_gateway.http.auth import tenant_for_header
from llm_gateway.http.presenter import error_payload
from llm_gateway.tenancy.auth import Authenticator, InvalidApiKeyError
from llm_gateway.tenancy.model import Tenant
from llm_gateway.tenancy.ports import TenantStoreUnavailableError
from llm_gateway.tools.gateway import ToolGateway
from llm_gateway.tools.model import ToolResult


def build_mcp_server(tools: ToolGateway) -> Server[Any]:
    async def list_tools(
        ctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        _tenant_of(ctx)
        specs = await tools.list_tools()
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=spec.qualified_name,
                    description=spec.description,
                    input_schema=dict(spec.input_schema),
                )
                for spec in specs
            ]
        )

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        result = await tools.call(_tenant_of(ctx), params.name, params.arguments or {})
        return _to_mcp(result)

    return Server("llm-gateway", on_list_tools=list_tools, on_call_tool=call_tool)


def build_mcp_app(
    server: Server[Any], authenticator: Authenticator
) -> tuple[ASGIApp, StreamableHTTPSessionManager]:
    """The ASGI app to mount at /mcp, and the session manager whose run() the host's
    lifespan must enter (mounted apps don't get their own lifespan)."""
    manager = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True)
    return _BearerAuth(manager.handle_request, authenticator), manager


class _BearerAuth:
    def __init__(self, app: ASGIApp, authenticator: Authenticator) -> None:
        self._app = app
        self._authenticator = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        try:
            tenant = await tenant_for_header(
                self._authenticator, Headers(scope=scope).get("authorization")
            )
        except InvalidApiKeyError:
            response = JSONResponse(
                error_payload("Invalid API key.", "invalid_request_error", "invalid_api_key"),
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        except TenantStoreUnavailableError:
            response = JSONResponse(
                error_payload("Authentication is temporarily unavailable.", "service_unavailable"),
                status_code=503,
            )
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["tenant"] = tenant
        await self._app(scope, receive, send)


def _tenant_of(ctx: Any) -> Tenant:
    tenant = getattr(getattr(ctx.request, "state", None), "tenant", None)
    if not isinstance(tenant, Tenant):  # only reachable if the auth layer was bypassed
        raise PermissionError("unauthenticated MCP request")
    return tenant


def _to_mcp(result: ToolResult) -> types.CallToolResult:
    body: Mapping[str, Any] = {
        "content": list(result.content),
        "is_error": result.is_error,
        "structured_content": result.structured_content,
    }
    return types.CallToolResult.model_validate(body)
