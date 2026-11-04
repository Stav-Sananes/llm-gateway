import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

import anyio
import pytest

from llm_gateway.approvals.desk import ApprovalDesk
from llm_gateway.approvals.hold import ApprovalHold, HoldSettings
from llm_gateway.audit.log import AuditLog
from llm_gateway.audit.ports import AuditUnavailableError
from llm_gateway.audit.redaction import REDACTED
from llm_gateway.policy.model import Decision, PolicyDecision, ToolCall
from llm_gateway.tenancy.model import Tenant
from llm_gateway.tools.gateway import (
    APPROVALS_UNAVAILABLE,
    AUDIT_UNAVAILABLE,
    ToolGateway,
    result_fingerprint,
)
from llm_gateway.tools.model import (
    ToolLimits,
    ToolResult,
    ToolSpec,
    UnknownToolError,
    UpstreamUnavailableError,
)
from tests.support.approvals import InMemoryApprovalStore
from tests.support.in_memory_audit_store import InMemoryAuditStore

pytestmark = pytest.mark.anyio

ACME = Tenant(UUID(int=1), "acme")
OK = ToolResult(content=({"type": "text", "text": "done"},))


class FakeUpstreams:
    """Behaves as HttpMcpUpstreams does: an unknown server raises UnknownToolError, an
    unreachable one UpstreamUnavailableError."""

    def __init__(
        self, result: ToolResult = OK, fails: bool = False, servers: tuple[str, ...] = ("sql", "fs")
    ) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._result = result
        self._fails = fails
        self._servers = servers

    async def list_tools(self) -> Sequence[ToolSpec]:
        return [ToolSpec("sql", "query", "Run SQL", {"type": "object"})]

    async def call(self, server: str, tool: str, arguments: Mapping[str, Any]) -> ToolResult:
        if server not in self._servers:
            raise UnknownToolError(f"no MCP server named {server!r}")
        self.calls.append((server, tool, dict(arguments)))
        if self._fails:
            raise UpstreamUnavailableError(f"{server}: ConnectError")
        return self._result


class FixedPolicy:
    def __init__(self, decision: Decision, *reasons: str) -> None:
        self._decision = PolicyDecision(decision, reasons or ("p",))
        self.calls: list[ToolCall] = []

    def decide(self, call: ToolCall) -> PolicyDecision:
        self.calls.append(call)
        return self._decision


class BrokenAuditStore(InMemoryAuditStore):
    """Accepts the first `works_for` appends, then is down."""

    def __init__(self, works_for: int = 0) -> None:
        super().__init__()
        self._works_for = works_for

    async def append(self, event: Any) -> Any:
        if self._works_for == 0:
            raise AuditUnavailableError("down")
        self._works_for -= 1
        return await super().append(event)


def a_gateway(
    policy: FixedPolicy,
    upstreams: FakeUpstreams | None = None,
    store: InMemoryAuditStore | None = None,
    limits: ToolLimits | None = None,
) -> tuple[ToolGateway, FakeUpstreams, InMemoryAuditStore]:
    upstreams, store = upstreams or FakeUpstreams(), store or InMemoryAuditStore()
    gateway = ToolGateway(upstreams, policy, AuditLog(store), limits or ToolLimits())
    return gateway, upstreams, store


async def actions(store: InMemoryAuditStore) -> list[dict[str, Any]]:
    return [json.loads(r.payload) async for r in store.records(ACME.id, up_to_seq=100)]


async def test_an_allowed_call_is_forwarded_and_audited_before_and_after() -> None:
    gateway, upstreams, store = a_gateway(FixedPolicy(Decision.ALLOW, "sql-select"))

    result = await gateway.call(ACME, "sql__query", {"sql": "SELECT 1"})

    assert result == OK
    assert upstreams.calls == [("sql", "query", {"sql": "SELECT 1"})]
    decided, completed = await actions(store)
    assert decided["action"] == "tool.call.decided"
    assert decided["details"] == {
        "tool": "sql__query",
        "arguments": {"sql": "SELECT 1"},
        "decision": "allow",
        "reasons": ["sql-select"],
    }
    assert completed["action"] == "tool.call.completed"
    assert completed["details"] == {
        "tool": "sql__query",
        "is_error": False,
        "withheld": False,
    } | result_fingerprint(OK)


async def test_the_policy_sees_the_tenant_server_tool_and_arguments() -> None:
    policy = FixedPolicy(Decision.ALLOW)
    gateway, _, _ = a_gateway(policy)

    await gateway.call(ACME, "fs__write_file", {"path": "/sandbox/a"})

    assert policy.calls == [ToolCall(ACME.id, "acme", "fs", "write_file", {"path": "/sandbox/a"})]


async def test_a_denied_call_never_reaches_the_upstream_and_says_why() -> None:
    gateway, upstreams, store = a_gateway(FixedPolicy(Decision.DENY, "no policy permits this call"))

    result = await gateway.call(ACME, "fs__write_file", {"path": "/etc/passwd"})

    assert result.is_error
    assert result.content[0]["text"] == "Denied by policy: no policy permits this call"
    assert upstreams.calls == []
    [decided] = await actions(store)
    assert decided["details"]["decision"] == "deny"


async def test_a_call_needing_approval_is_refused_when_approvals_arent_set_up() -> None:
    gateway, upstreams, store = a_gateway(FixedPolicy(Decision.REQUIRE_APPROVAL, "fs-move"))

    result = await gateway.call(ACME, "fs__move_file", {"source": "/sandbox/a"})

    assert result.is_error
    assert "approval" in result.content[0]["text"]
    assert upstreams.calls == []
    [decided] = await actions(store)
    assert decided["details"]["decision"] == "require_approval"


async def test_when_the_audit_log_is_down_the_call_does_not_run() -> None:
    gateway, upstreams, _ = a_gateway(FixedPolicy(Decision.ALLOW), store=BrokenAuditStore())

    result = await gateway.call(ACME, "sql__query", {"sql": "SELECT 1"})

    assert result == ToolResult.error(AUDIT_UNAVAILABLE)
    assert upstreams.calls == []


async def test_a_call_that_ran_returns_its_result_even_if_the_outcome_cannot_be_audited() -> None:
    # Reporting a failure would invite a retry of a call that already happened.
    store = BrokenAuditStore(works_for=1)  # the decision is recorded, the outcome isn't
    gateway, upstreams, _ = a_gateway(FixedPolicy(Decision.ALLOW), store=store)

    result = await gateway.call(ACME, "fs__write_file", {"path": "/sandbox/a"})

    assert result == OK
    assert len(upstreams.calls) == 1
    assert [a["action"] for a in await actions(store)] == ["tool.call.decided"]


async def test_an_upstream_error_result_is_passed_on_and_audited_as_an_error() -> None:
    failed = ToolResult.error("no such table")
    gateway, _, store = a_gateway(FixedPolicy(Decision.ALLOW), FakeUpstreams(result=failed))

    result = await gateway.call(ACME, "sql__query", {"sql": "SELECT 1"})

    assert result == failed
    _, completed = await actions(store)
    assert completed["details"]["is_error"] is True


async def test_a_call_to_an_unconfigured_server_is_an_error_and_audited() -> None:
    gateway, _, store = a_gateway(FixedPolicy(Decision.ALLOW), FakeUpstreams(servers=("sql",)))

    result = await gateway.call(ACME, "nope__query", {})

    assert result == ToolResult.error("nope__query is unavailable.")
    _, failed = await actions(store)
    assert failed["action"] == "tool.call.failed"
    assert failed["details"] == {"tool": "nope__query", "error": "no MCP server named 'nope'"}


def test_the_result_fingerprint_identifies_the_result() -> None:
    small, other = ToolResult(content=({"type": "text", "text": "a"},)), ToolResult.error("a")
    big = ToolResult(content=({"type": "text", "text": "a" * 1000},))

    assert result_fingerprint(small) == result_fingerprint(ToolResult(content=small.content))
    assert result_fingerprint(small)["result_sha256"] != result_fingerprint(other)["result_sha256"]
    assert (
        result_fingerprint(big)["result_bytes"] - result_fingerprint(small)["result_bytes"] == 999
    )


async def test_oversized_arguments_are_refused_before_any_policy_and_audited() -> None:
    policy = FixedPolicy(Decision.ALLOW)
    limits = ToolLimits(max_argument_bytes=100)
    gateway, upstreams, store = a_gateway(policy, limits=limits)

    result = await gateway.call(ACME, "sql__query", {"sql": "SELECT " + "1," * 100})

    assert result.is_error and "the limit is 100" in result.content[0]["text"]
    assert policy.calls == [] and upstreams.calls == []
    [rejected] = await actions(store)
    assert rejected["action"] == "tool.call.rejected"
    assert rejected["details"]["tool"] == "sql__query"


async def test_an_oversized_result_is_withheld_but_fingerprinted() -> None:
    big = ToolResult(content=({"type": "text", "text": "x" * 500},))
    limits = ToolLimits(max_result_bytes=100)
    gateway, _, store = a_gateway(FixedPolicy(Decision.ALLOW), FakeUpstreams(big), limits=limits)

    result = await gateway.call(ACME, "sql__query", {"sql": "SELECT 1"})

    assert result.is_error and "the limit is 100" in result.content[0]["text"]
    _, completed = await actions(store)
    assert completed["details"]["withheld"] is True
    assert completed["details"]["result_sha256"] == result_fingerprint(big)["result_sha256"]


async def test_an_unreachable_upstream_is_an_error_result_and_audited() -> None:
    gateway, _, store = a_gateway(FixedPolicy(Decision.ALLOW), FakeUpstreams(fails=True))

    result = await gateway.call(ACME, "sql__query", {"sql": "SELECT 1"})

    assert result == ToolResult.error("sql__query is unavailable.")
    assert [a["action"] for a in await actions(store)] == ["tool.call.decided", "tool.call.failed"]


@pytest.mark.parametrize("name", ["query", "__query", "sql__", ""])
async def test_a_name_that_is_not_server__tool_is_an_audited_error_without_a_decision(
    name: str,
) -> None:
    policy = FixedPolicy(Decision.ALLOW)
    gateway, upstreams, store = a_gateway(policy)

    result = await gateway.call(ACME, name, {})

    assert result.is_error
    assert policy.calls == [] and upstreams.calls == []
    [rejected] = await actions(store)
    assert rejected["action"] == "tool.call.rejected"


async def test_secrets_in_arguments_are_redacted_in_the_audit_log() -> None:
    gateway, _, store = a_gateway(FixedPolicy(Decision.ALLOW))

    await gateway.call(ACME, "sql__query", {"sql": "SELECT 1", "password": "hunter2"})

    [decided, _] = await actions(store)
    assert decided["details"]["arguments"]["password"] == REDACTED


# The approval hold


async def a_held_call(
    decide: str | None, store: InMemoryApprovalStore | None = None, max_pending: int = 10
) -> tuple[ToolResult, FakeUpstreams, list[dict[str, Any]]]:
    """Call a tool that needs approval while an admin approves or rejects it (None: nobody
    decides and the hold times out)."""
    approvals = store or InMemoryApprovalStore()
    hold = ApprovalHold(
        approvals, HoldSettings(timeout_s=0.2, poll_s=0.001, max_pending_per_tenant=max_pending)
    )
    upstreams, audit = FakeUpstreams(), InMemoryAuditStore()
    gateway = ToolGateway(
        upstreams, FixedPolicy(Decision.REQUIRE_APPROVAL, "fs-move"), AuditLog(audit),
        hold=hold,
    )  # fmt: skip
    result: list[ToolResult] = []

    async def call() -> None:
        result.append(await gateway.call(ACME, "fs__move_file", MOVE))

    with anyio.fail_after(2):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(call)
            if decide is not None:
                await approvals.held.wait()
                (approval,) = approvals.approvals.values()
                await getattr(ApprovalDesk(approvals), decide)(approval.id, "dana", "ok")
    return result[0], upstreams, await actions(audit)


MOVE = {"source": "/sandbox/a", "destination": "/sandbox/b"}


async def test_an_approved_call_runs_after_its_approval_is_audited() -> None:
    result, upstreams, audited = await a_held_call("approve")

    assert result == OK
    assert upstreams.calls == [("fs", "move_file", MOVE)]
    assert [a["action"] for a in audited] == [
        "tool.call.decided", "tool.call.approval", "tool.call.completed"
    ]  # fmt: skip
    approval = audited[1]["details"]
    assert (approval["status"], approval["decided_by"]) == ("approved", "dana")


async def test_a_rejected_call_never_runs() -> None:
    result, upstreams, audited = await a_held_call("reject")

    assert result.is_error and result.content[0]["text"] == "Rejected by dana: ok"
    assert upstreams.calls == []
    assert audited[-1]["details"]["status"] == "rejected"


async def test_a_call_nobody_approves_in_time_never_runs() -> None:
    result, upstreams, audited = await a_held_call(None)

    assert result.is_error and "within 0.2 seconds" in result.content[0]["text"]
    assert upstreams.calls == []
    assert audited[-1]["details"]["status"] == "expired"


async def test_when_approvals_are_down_a_held_call_does_not_run() -> None:
    store = InMemoryApprovalStore()
    store.down = True

    result, upstreams, audited = await a_held_call(None, store)

    assert result.content[0]["text"] == APPROVALS_UNAVAILABLE
    assert upstreams.calls == []
    assert [a["action"] for a in audited] == ["tool.call.decided"]


async def test_a_tenant_with_too_many_held_calls_is_refused() -> None:
    result, upstreams, _ = await a_held_call(None, max_pending=0)

    assert "Too many" in result.content[0]["text"]
    assert upstreams.calls == []
