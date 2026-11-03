"""The MCP aggregator's vocabulary, independent of the MCP SDK's types."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# Clients see every upstream tool as `<server>__<tool>`.
SEPARATOR = "__"


@dataclass(frozen=True)
class ToolSpec:
    server: str
    name: str
    description: str | None
    input_schema: Mapping[str, Any]

    @property
    def qualified_name(self) -> str:
        return f"{self.server}{SEPARATOR}{self.name}"


@dataclass(frozen=True)
class ToolResult:
    """An MCP tool result as plain data: content blocks as dicts, in MCP's wire shape."""

    content: Sequence[Mapping[str, Any]]
    is_error: bool = False
    structured_content: Mapping[str, Any] | None = field(default=None)

    @classmethod
    def error(cls, message: str) -> "ToolResult":
        return cls(content=({"type": "text", "text": message},), is_error=True)


class UnknownToolError(Exception):
    """The name doesn't follow `server__tool`, or names no configured server."""


class UpstreamUnavailableError(Exception):
    """An upstream MCP server could not be reached or didn't answer in time."""


class ArgumentsTooLargeError(Exception):
    """A call's arguments are over the size the gateway will judge."""


@dataclass(frozen=True)
class ToolLimits:
    # Deriving policy facts (parsing SQL) costs time in proportion to the arguments' size,
    # so oversized arguments are refused before any policy runs.
    max_argument_bytes: int = 64 * 1024
    # An upstream result over this is withheld from the client (and still fingerprinted).
    max_result_bytes: int = 1024 * 1024


def is_valid_server_name(name: str) -> bool:
    """A server name must survive the round trip through `server__tool`: with a trailing
    "_", `a_` + `__` + `query` reads back as server `a`, tool `_query`."""
    return bool(name) and SEPARATOR not in name and not name.endswith("_")


def split_qualified_name(qualified_name: str) -> tuple[str, str]:
    server, separator, tool = qualified_name.partition(SEPARATOR)
    if not separator or not server or not tool:
        raise UnknownToolError(
            f"tool names look like server{SEPARATOR}tool, got {qualified_name!r}"
        )
    return server, tool
