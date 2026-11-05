"""The /mcp endpoint: one MCP server (Streamable HTTP) in front of every upstream server.

Stateless: each request is self-contained, so any replica can serve any request. A small
ASGI layer authenticates the bearer key before the MCP SDK sees the request, and leaves the
tenant in the request state for the tool handlers.

Another one watches for the client hanging up. The SDK runs tool handlers in its own task
group, so a client that disconnects never cancels its handler. A call held for approval
would then go on waiting, and run once approved, with nobody there for the result. The
handler learns that its client has left from a ClientPresence, through a context
variable: tasks inherit the context of the request that started them.
"""

from collections.abc import Mapping
from contextvars import ContextVar
from typing import Any

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from llm_gateway.approvals.hold import STAYS_WAITING, Waiter
from llm_gateway.http.admission import Admission, AtCapacityError, LoopLag
from llm_gateway.http.auth import tenant_for_header
from llm_gateway.http.presenter import error_payload
from llm_gateway.tenancy.auth import Authenticator, InvalidApiKeyError
from llm_gateway.tenancy.model import Tenant
from llm_gateway.tenancy.ports import TenantStoreUnavailableError
from llm_gateway.tools.gateway import ToolGateway
from llm_gateway.tools.model import ToolResult


def build_mcp_server(tools: ToolGateway, admission: Admission | None = None) -> Server[Any]:
    admission = admission or Admission(LoopLag())

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
        tenant = _tenant_of(ctx)
        try:
            ticket = admission.admit()  # after auth: a client without a key holds nothing
        except AtCapacityError:
            return _to_mcp(ToolResult.error("The gateway is at capacity. Retry shortly."))
        try:
            result = await tools.call(tenant, params.name, params.arguments or {}, _client.get())
        finally:
            ticket.release()
        return _to_mcp(result)

    return Server("llm-gateway", on_list_tools=list_tools, on_call_tool=call_tool)


def build_mcp_app(
    server: Server[Any], authenticator: Authenticator
) -> tuple[ASGIApp, StreamableHTTPSessionManager]:
    """The ASGI app to mount at /mcp, and the session manager whose run() the host's
    lifespan must enter (mounted apps don't get their own lifespan)."""
    manager = StreamableHTTPSessionManager(app=server, stateless=True, json_response=True)
    return _BearerAuth(_WatchDisconnect(manager.handle_request), authenticator), manager


class ClientPresence:
    """Whether the client of the current /mcp request is still connected."""

    def __init__(self) -> None:
        self._left = anyio.Event()

    def has_left(self) -> bool:
        return self._left.is_set()

    def leave(self) -> None:
        self._left.set()

    async def wait_until_left(self) -> None:
        await self._left.wait()


_client: ContextVar[Waiter] = ContextVar("mcp_client", default=STAYS_WAITING)


class _WatchDisconnect:
    """Once the request body has been read, the only message left for the app is
    http.disconnect. This layer reads it itself, marks the ClientPresence, and hands the
    app a receive() that reports the disconnect too."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        presence = ClientPresence()
        body_read = anyio.Event()

        async def app_receive() -> Message:
            if body_read.is_set():
                await presence.wait_until_left()
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.disconnect":
                presence.leave()
            if message["type"] == "http.disconnect" or not message.get("more_body", False):
                body_read.set()
            return message

        async def watch() -> None:
            await body_read.wait()
            if not presence.has_left() and (await receive())["type"] == "http.disconnect":
                presence.leave()

        token = _client.set(presence)
        try:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(watch)
                await self._app(scope, app_receive, send)
                tasks.cancel_scope.cancel()  # answered: the client's leaving no longer matters
        finally:
            _client.reset(token)


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
