"""The MCP aggregator's use case: every tool call is decided, audited, then forwarded.

    check name and size ─► decide (Cedar) ─► audit "tool.call.decided"
        ─► denied → refusal
        ─► needs approval → held until a human decides ─► audit "tool.call.approval"
              ├► approved → upstream ─► audit the outcome
              └► rejected / expired → refusal
        └► allowed → upstream ─► audit the outcome

The decision is written to the audit chain *before* the call runs; if that write fails, the
call doesn't run and the client gets an error result. Once the upstream has run, the result
is returned even if its outcome can't be audited: reporting a failure would invite a retry
of a call that already happened.
"""

import hashlib
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

import anyio

from llm_gateway.approvals.hold import STAYS_WAITING, ApprovalHold, Waiter
from llm_gateway.approvals.model import ApprovalStatus
from llm_gateway.approvals.ports import ApprovalUnavailableError, TooManyPendingError
from llm_gateway.audit.chain import UnauditableEventError
from llm_gateway.audit.log import AuditLog
from llm_gateway.audit.ports import AuditUnavailableError
from llm_gateway.policy.model import Decision, ToolCall
from llm_gateway.policy.ports import PolicyEngine
from llm_gateway.tenancy.model import Tenant
from llm_gateway.tools.model import (
    ArgumentsTooLargeError,
    ToolLimits,
    ToolResult,
    ToolSpec,
    UnknownToolError,
    UpstreamUnavailableError,
    split_qualified_name,
)
from llm_gateway.tools.ports import UpstreamServers

logger = logging.getLogger(__name__)

AUDIT_UNAVAILABLE = "The audit log is unavailable, so the call was not made."
APPROVALS_UNAVAILABLE = "Approvals are unavailable, so the call was not made."
_AUDIT_FAILURES = (AuditUnavailableError, UnauditableEventError)
DEFAULT_LIMITS = ToolLimits()
_AUDITED_NAME_CHARS = 200  # a rejected name is client input of any length


class ToolGateway:
    def __init__(
        self,
        upstreams: UpstreamServers,
        policy: PolicyEngine,
        audit: AuditLog,
        limits: ToolLimits = DEFAULT_LIMITS,
        hold: ApprovalHold | None = None,  # None: calls needing approval are refused
    ) -> None:
        self._upstreams = upstreams
        self._policy = policy
        self._audit = audit
        self._limits = limits
        self._hold = hold

    async def list_tools(self) -> Sequence[ToolSpec]:
        return await self._upstreams.list_tools()

    async def call(
        self,
        tenant: Tenant,
        qualified_name: str,
        arguments: Mapping[str, Any],
        client: Waiter = STAYS_WAITING,  # a held call stops waiting once its client has left
    ) -> ToolResult:
        try:
            return await self._governed_call(tenant, qualified_name, arguments, client)
        except _AUDIT_FAILURES as error:
            logger.error("tool call %s not made, audit failed: %s", qualified_name, error)
            return ToolResult.error(AUDIT_UNAVAILABLE)

    async def _governed_call(
        self, tenant: Tenant, qualified_name: str, arguments: Mapping[str, Any], client: Waiter
    ) -> ToolResult:
        try:
            call = self._checked_call(tenant, qualified_name, arguments)
        except (UnknownToolError, ArgumentsTooLargeError) as error:
            await self._audit.record(
                tenant.id,
                "tool.call.rejected",
                {"tool": qualified_name[:_AUDITED_NAME_CHARS], "reason": str(error)},
            )
            return ToolResult.error(str(error))
        # Off the event loop: deriving facts parses SQL, which takes a while on big inputs.
        decision = await anyio.to_thread.run_sync(self._policy.decide, call)
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
            return await self._held(tenant, call, decision.reasons, client)
        return await self._forward(tenant, call)

    async def _held(
        self, tenant: Tenant, call: ToolCall, reasons: Sequence[str], client: Waiter
    ) -> ToolResult:
        if self._hold is None:
            return ToolResult.error("This call needs a human's approval, which isn't set up.")
        try:
            approval = await self._hold.wait(call, reasons, client)
        except TooManyPendingError:
            return ToolResult.error(
                "Too many of this tenant's calls are already waiting for approval."
            )
        except ApprovalUnavailableError as error:
            logger.error("tool call %s not made, approvals failed: %s", call.qualified_name, error)
            return ToolResult.error(APPROVALS_UNAVAILABLE)
        # On the chain before anything runs, like the policy decision.
        await self._audit.record(
            tenant.id,
            "tool.call.approval",
            {
                "tool": call.qualified_name,
                "approval_id": str(approval.id),
                "status": approval.status.value,
                "decided_by": approval.decided_by,
                "note": approval.note,
            },
        )
        if approval.status is ApprovalStatus.APPROVED:
            return await self._forward(tenant, call)
        if approval.status is ApprovalStatus.REJECTED:
            why = f": {approval.note}" if approval.note else ""
            return ToolResult.error(f"Rejected by {approval.decided_by}{why}")
        if approval.status is ApprovalStatus.ABANDONED:
            return ToolResult.error("The call was abandoned: its client stopped waiting.")
        return ToolResult.error(
            f"Nobody approved this call within {self._hold.timeout_s:g} seconds."
        )

    def _checked_call(
        self, tenant: Tenant, qualified_name: str, arguments: Mapping[str, Any]
    ) -> ToolCall:
        server, tool = split_qualified_name(qualified_name)
        size = _json_size(arguments)
        if size > self._limits.max_argument_bytes:
            raise ArgumentsTooLargeError(
                f"arguments are {size} bytes; the limit is {self._limits.max_argument_bytes}"
            )
        return ToolCall(tenant.id, tenant.name, server, tool, arguments)

    async def _forward(self, tenant: Tenant, call: ToolCall) -> ToolResult:
        try:
            result = await self._upstreams.call(call.server, call.tool, call.arguments)
        except (UnknownToolError, UpstreamUnavailableError) as error:
            await self._audit.record(
                tenant.id, "tool.call.failed", {"tool": call.qualified_name, "error": str(error)}
            )
            logger.warning("tool call %s failed: %s", call.qualified_name, error)
            return ToolResult.error(f"{call.qualified_name} is unavailable.")
        fingerprint = result_fingerprint(result)
        withheld = fingerprint["result_bytes"] > self._limits.max_result_bytes
        await self._record_outcome(tenant, call, result, fingerprint | {"withheld": withheld})
        if withheld:
            return ToolResult.error(
                f"{call.qualified_name} returned {fingerprint['result_bytes']} bytes; "
                f"the limit is {self._limits.max_result_bytes}."
            )
        return result

    async def _record_outcome(
        self, tenant: Tenant, call: ToolCall, result: ToolResult, details: dict[str, Any]
    ) -> None:
        try:
            await self._audit.record(
                tenant.id,
                "tool.call.completed",
                {"tool": call.qualified_name, "is_error": result.is_error} | details,
            )
        except _AUDIT_FAILURES as error:
            # The call ran and its decision is on the chain; only the outcome is missing.
            logger.error("tool call %s ran, outcome not audited: %s", call.qualified_name, error)


def result_fingerprint(result: ToolResult) -> dict[str, Any]:
    """The result itself may be large or sensitive; the chain keeps its size and SHA-256,
    which is enough to prove later what was returned."""
    payload = json.dumps(
        {
            "content": list(result.content),
            "is_error": result.is_error,
            "structured": result.structured_content,
        },
        sort_keys=True,
        default=str,
    ).encode()
    return {"result_bytes": len(payload), "result_sha256": hashlib.sha256(payload).hexdigest()}


def _json_size(arguments: Mapping[str, Any]) -> int:
    return len(json.dumps(arguments, default=str).encode())
