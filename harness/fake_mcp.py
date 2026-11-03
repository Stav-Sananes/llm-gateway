"""Fake upstream MCP servers (sql and fs) that record every call they get.

They stand in for the demo servers in tests: the gateway talks real MCP over Streamable
HTTP to them, and the logs show which calls actually reached an upstream.
"""

from dataclasses import dataclass, field
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send


@dataclass
class McpCallLog:
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


def create_fake_sql_server(log: McpCallLog) -> Starlette:
    server = MCPServer("sql")

    @server.tool()
    def query(sql: str) -> str:
        """Run a read-only SQL query."""
        log.calls.append(("query", {"sql": sql}))
        return "id | total\n1  | 42"

    return server.streamable_http_app(stateless_http=True, json_response=True)


def create_fake_fs_server(log: McpCallLog) -> Starlette:
    server = MCPServer("fs")

    @server.tool()
    def write_file(path: str, content: str) -> str:
        """Write a text file."""
        log.calls.append(("write_file", {"path": path, "content": content}))
        return f"wrote {len(content)} bytes to {path}"

    @server.tool()
    def move_file(source: str, destination: str) -> str:
        """Move a file."""
        log.calls.append(("move_file", {"source": source, "destination": destination}))
        return "moved"

    @server.tool()
    def read_text_file(path: str) -> str:
        """Read a text file."""
        log.calls.append(("read_text_file", {"path": path}))
        raise ValueError(f"no such file: {path}")  # an MCP-level error result

    return server.streamable_http_app(stateless_http=True, json_response=True)


def slowed(app: ASGIApp, per_request_s: float) -> ASGIApp:
    """`app`, answering every HTTP request only after `per_request_s`.

    Each request stays under a per-read timeout, but one MCP operation is several requests
    (initialize, initialized, the call), so only an overall deadline stops it in time.
    """

    async def slow_app(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            await anyio.sleep(per_request_s)
        await app(scope, receive, send)

    return slow_app
