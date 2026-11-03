import json
from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

import pytest

from llm_gateway.audit.log import AuditLog
from llm_gateway.audit.ports import AuditUnavailableError
from llm_gateway.audit.redaction import REDACTED
from llm_gateway.policy.model import Decision, PolicyDecision, ToolCall
from llm_gateway.tenancy.model import Tenant
from llm_gateway.tools.gateway import ToolGateway
from llm_gateway.tools.model import ToolResult, ToolSpec, UpstreamUnavailableError
from tests.support.in_memory_audit_store import InMemoryAuditStore

pytestmark = pytest.mark.anyio

ACME = Tenant(UUID(int=1), "acme")
OK = ToolResult(content=({"type": "text", "text": "done"},))


class FakeUpstreams:
    def __init__(self, result: ToolResult = OK, fails: bool = False) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._result = result
        self._fails = fails

    async def list_tools(self) -> Sequence[ToolSpec]:
        return [ToolSpec("sql", "query", "Run SQL", {"type": "object"})]

    async def call(self, server: str, tool: str, arguments: Mapping[str, Any]) -> ToolResult:
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
    async def append(self, event: Any) -> Any:
        raise AuditUnavailableError("down")


def a_gateway(
    policy: FixedPolicy,
    upstreams: FakeUpstreams | None = None,
    store: InMemoryAuditStore | None = None,
) -> tuple[ToolGateway, FakeUpstreams, InMemoryAuditStore]:
    upstreams, store = upstreams or FakeUpstreams(), store or InMemoryAuditStore()
    return ToolGateway(upstreams, policy, AuditLog(store)), upstreams, store


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
    assert completed["details"]["is_error"] is False
    assert len(completed["details"]["result_sha256"]) == 64


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


async def test_a_call_needing_approval_is_refused_for_now() -> None:
    gateway, upstreams, store = a_gateway(FixedPolicy(Decision.REQUIRE_APPROVAL, "fs-move"))

    result = await gateway.call(ACME, "fs__move_file", {"source": "/sandbox/a"})

    assert result.is_error
    assert "approval" in result.content[0]["text"]
    assert upstreams.calls == []
    [decided] = await actions(store)
    assert decided["details"]["decision"] == "require_approval"


async def test_when_the_audit_log_is_down_the_call_does_not_run() -> None:
    gateway, upstreams, _ = a_gateway(FixedPolicy(Decision.ALLOW), store=BrokenAuditStore())

    with pytest.raises(AuditUnavailableError):
        await gateway.call(ACME, "sql__query", {"sql": "SELECT 1"})

    assert upstreams.calls == []


async def test_an_unreachable_upstream_is_an_error_result_and_audited() -> None:
    gateway, _, store = a_gateway(FixedPolicy(Decision.ALLOW), FakeUpstreams(fails=True))

    result = await gateway.call(ACME, "sql__query", {"sql": "SELECT 1"})

    assert result == ToolResult.error("sql__query is unavailable.")
    assert [a["action"] for a in await actions(store)] == ["tool.call.decided", "tool.call.failed"]


@pytest.mark.parametrize("name", ["query", "__query", "sql__", ""])
async def test_a_name_that_is_not_server__tool_is_an_error_without_a_decision(name: str) -> None:
    policy = FixedPolicy(Decision.ALLOW)
    gateway, upstreams, _ = a_gateway(policy)

    result = await gateway.call(ACME, name, {})

    assert result.is_error
    assert policy.calls == [] and upstreams.calls == []


async def test_secrets_in_arguments_are_redacted_in_the_audit_log() -> None:
    gateway, _, store = a_gateway(FixedPolicy(Decision.ALLOW))

    await gateway.call(ACME, "sql__query", {"sql": "SELECT 1", "password": "hunter2"})

    [decided, _] = await actions(store)
    assert decided["details"]["arguments"]["password"] == REDACTED
