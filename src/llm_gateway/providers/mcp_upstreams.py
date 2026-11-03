"""UpstreamServers over MCP Streamable HTTP, through the official MCP SDK.

One short-lived client session per operation: simple, nothing to reconnect, and every
operation is bounded by one deadline. (A pooled session per server is a later optimisation.)
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

import anyio
import httpx2
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from llm_gateway.tools.model import (
    SEPARATOR,
    ToolResult,
    ToolSpec,
    UnknownToolError,
    UpstreamUnavailableError,
)

logger = logging.getLogger(__name__)


class HttpMcpUpstreams:
    def __init__(self, servers: Mapping[str, str], timeout_s: float) -> None:
        if bad := [name for name in servers if SEPARATOR in name or not name]:
            raise ValueError(
                f"MCP server names must be non-empty and not contain {SEPARATOR!r}: {bad}"
            )
        self._servers = dict(servers)
        self._timeout_s = timeout_s

    async def list_tools(self) -> Sequence[ToolSpec]:
        listed = await asyncio.gather(
            *(self._tools_of(name) for name in self._servers), return_exceptions=True
        )
        tools: list[ToolSpec] = []
        for name, result in zip(self._servers, listed, strict=True):
            if isinstance(result, BaseException):
                logger.warning("MCP server %s left out of the tool list: %s", name, result)
                continue
            tools.extend(result)
        return tools

    async def call(self, server: str, tool: str, arguments: Mapping[str, Any]) -> ToolResult:
        url = self._url(server)
        try:
            with anyio.fail_after(self._timeout_s):
                async with self._session(url) as session:
                    result = await session.call_tool(tool, dict(arguments))
        except Exception as error:
            raise UpstreamUnavailableError(f"{server}: {type(error).__name__}") from error
        if not isinstance(result, types.CallToolResult):
            raise UpstreamUnavailableError(f"{server}: unexpected result {type(result).__name__}")
        return ToolResult(
            content=tuple(
                block.model_dump(mode="json", exclude_none=True) for block in result.content
            ),
            is_error=bool(result.is_error),
            structured_content=result.structured_content,
        )

    async def _tools_of(self, server: str) -> list[ToolSpec]:
        with anyio.fail_after(self._timeout_s):
            async with self._session(self._url(server)) as session:
                listed = await session.list_tools()
        return [ToolSpec(server, t.name, t.description, t.input_schema) for t in listed.tools]

    def _url(self, server: str) -> str:
        try:
            return self._servers[server]
        except KeyError:
            raise UnknownToolError(f"no MCP server named {server!r}") from None

    @asynccontextmanager
    async def _session(self, url: str) -> AsyncIterator[ClientSession]:
        timeout = httpx2.Timeout(self._timeout_s, connect=min(self._timeout_s, 2.0))
        async with (
            create_mcp_http_client(timeout=timeout) as http_client,
            streamable_http_client(url, http_client=http_client) as (read, write),
            ClientSession(read, write, read_timeout_seconds=self._timeout_s) as session,
        ):
            await session.initialize()
            yield session
