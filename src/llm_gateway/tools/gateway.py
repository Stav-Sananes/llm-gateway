"""The MCP aggregator's use case: every tool call is decided, audited, then forwarded.

    decide (Cedar) ─► audit "tool.call.decided" ─► denied / needs approval → refusal
                                                └► allowed → upstream ─► audit the outcome

The decision is written to the audit chain *before* the call runs; if the audit write
fails, the call doesn't run (AuditUnavailableError propagates).
"""

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from llm_gateway.audit.log import AuditLog
from llm_gateway.policy.model import Decision, ToolCall
from llm_gateway.policy.ports import PolicyEngine
from llm_gateway.tenancy.model import Tenant
from llm_gateway.tools.model import (
    ToolResult,
    ToolSpec,
    UnknownToolError,
    UpstreamUnavailableError,
    split_qualified_name,
)
from llm_gateway.tools.ports import UpstreamServers

logger = logging.getLogger(__name__)


class ToolGateway:
    def __init__(self, upstreams: UpstreamServers, policy: PolicyEngine, audit: AuditLog) -> None:
        self._upstreams = upstreams
        self._policy = policy
        self._audit = audit

    async def list_tools(self) -> Sequence[ToolSpec]:
        return await self._upstreams.list_tools()

    async def call(
        self, tenant: Tenant, qualified_name: str, arguments: Mapping[str, Any]
    ) -> ToolResult:
        try:
            server, tool = split_qualified_name(qualified_name)
        except UnknownToolError as error:
            return ToolResult.error(str(error))
        call = ToolCall(tenant.id, tenant.name, server, tool, arguments)
        decision = self._policy.decide(call)
        await self._audit.record(
            tenant.id,
            "tool.call.decided",
            {
                "tool": qualified_name,
                "arguments": dict(arguments),
                "decision": decision.decision.value,
                "reasons": list(decision.reasons),
            },
        )
        if decision.decision is Decision.DENY:
            return ToolResult.error(f"Denied by policy: {', '.join(decision.reasons)}")
        if decision.decision is Decision.REQUIRE_APPROVAL:
            # The approval hold (build step 9) will park the call here instead.
            return ToolResult.error(
                "This call needs a human's approval, which isn't available yet."
            )
        return await self._forward(tenant, call)

    async def _forward(self, tenant: Tenant, call: ToolCall) -> ToolResult:
        try:
            result = await self._upstreams.call(call.server, call.tool, call.arguments)
        except (UnknownToolError, UpstreamUnavailableError) as error:
            await self._audit.record(
                tenant.id, "tool.call.failed", {"tool": call.qualified_name, "error": str(error)}
            )
            logger.warning("tool call %s failed: %s", call.qualified_name, error)
            return ToolResult.error(f"{call.qualified_name} is unavailable.")
        await self._audit.record(
            tenant.id,
            "tool.call.completed",
            {"tool": call.qualified_name, "is_error": result.is_error} | _fingerprint(result),
        )
        return result


def _fingerprint(result: ToolResult) -> dict[str, Any]:
    # The result itself may be large or sensitive; the chain keeps its size and digest,
    # which is enough to prove later what was returned.
    payload = json.dumps(
        {"content": list(result.content), "structured": result.structured_content},
        sort_keys=True,
        default=str,
    ).encode()
    return {"result_bytes": len(payload), "result_sha256": hashlib.sha256(payload).hexdigest()}
