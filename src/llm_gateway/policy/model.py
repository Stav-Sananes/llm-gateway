"""What a tool-call policy decides about, and what it can answer."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class ToolCall:
    tenant_id: UUID
    tenant_name: str
    server: str  # the upstream MCP server
    tool: str  # the tool's name on that server
    arguments: Mapping[str, Any]

    @property
    def qualified_name(self) -> str:
        """The name clients see through the aggregator: `server__tool`."""
        return f"{self.server}__{self.tool}"


@dataclass(frozen=True)
class PolicyDecision:
    decision: Decision
    reasons: tuple[str, ...]  # ids of the policies that decided, or why none could
