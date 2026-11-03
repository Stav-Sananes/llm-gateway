"""What the MCP aggregator needs from the upstream MCP servers."""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from llm_gateway.tools.model import ToolResult, ToolSpec


class UpstreamServers(Protocol):
    async def list_tools(self) -> Sequence[ToolSpec]:
        """Every tool of every reachable server. Unreachable servers are left out."""
        ...

    async def call(self, server: str, tool: str, arguments: Mapping[str, Any]) -> ToolResult:
        """Raises UnknownToolError (no such server) or UpstreamUnavailableError."""
        ...
