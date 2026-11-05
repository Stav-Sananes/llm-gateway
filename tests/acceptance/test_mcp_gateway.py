"""/mcp end to end: an MCP client, the gateway (auth, Cedar, audit chain) and fake upstreams."""

import asyncio
import contextlib
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client
from sqlalchemy import select

from harness.fake_mcp import McpCallLog, create_fake_fs_server, create_fake_sql_server, slowed
from llm_gateway.audit.log import AuditLog, Verification
from llm_gateway.cli import main as cli
from llm_gateway.config import Settings
from llm_gateway.main import build_app, build_engine
from llm_gateway.storage.audit_store import PostgresAuditStore
from llm_gateway.storage.tables import approvals as approvals_table
from llm_gateway.storage.tenant_store import PostgresTenantStore
from tests.support.backing_services import new_api_key
from tests.support.servers import serving

CLIENT_TIMEOUT_S = 15  # a broken gateway deadline fails the test instead of hanging it
MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
APPROVAL_TIMEOUT_S = 3.0
MOVE = {"source": "/sandbox/a", "destination": "/sandbox/b"}


@dataclass
class Gateway:
    url: str
    api_key: str
    sql: McpCallLog
    fs: McpCallLog


@contextmanager
def running_gateway(services: Settings, **overrides: Any) -> Iterator[Gateway]:
    sql_log, fs_log = McpCallLog(), McpCallLog()
    with (
        serving(create_fake_sql_server(sql_log)) as sql_url,
        serving(create_fake_fs_server(fs_log)) as fs_url,
    ):
        settings = services.model_copy(
            update={
                "mcp_servers": {"sql": f"{sql_url}/mcp", "fs": f"{fs_url}/mcp"},
                "approval_timeout_s": APPROVAL_TIMEOUT_S,
                "approval_poll_s": 0.05,
            }
            | overrides
        )
        with serving(build_app(settings)) as url:
            yield Gateway(f"{url}/mcp", new_api_key(services), sql_log, fs_log)


@pytest.fixture
def gateway(services: Settings) -> Iterator[Gateway]:
    with running_gateway(services) as gateway:
        yield gateway


def mcp(
    gateway: Gateway, *calls: tuple[str, dict[str, Any]]
) -> tuple[list[str], list[types.CallToolResult]]:
    """Connect as an MCP client with the tenant's key, list the tools, make the calls."""

    async def run() -> tuple[list[str], list[types.CallToolResult]]:
        headers = {"Authorization": f"Bearer {gateway.api_key}"}
        async with (
            create_mcp_http_client(headers=headers) as http_client,
            streamable_http_client(gateway.url, http_client=http_client) as (read, write),
            ClientSession(read, write, read_timeout_seconds=CLIENT_TIMEOUT_S) as session,
        ):
            await session.initialize()
            tools = sorted(tool.name for tool in (await session.list_tools()).tools)
            results = []
            for name, arguments in calls:
                result = await session.call_tool(name, arguments)
                assert isinstance(result, types.CallToolResult)
                results.append(result)
            return tools, results

    return asyncio.run(run())


def text(result: types.CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    return block.text


def audit_trail(services: Settings) -> tuple[list[dict[str, Any]], Verification]:
    async def read() -> tuple[list[dict[str, Any]], Verification]:
        engine = build_engine(services)
        try:
            tenant = await PostgresTenantStore(engine).tenant_named("acme")
            assert tenant is not None
            store = PostgresAuditStore(engine)
            records = store.records(tenant.id, up_to_seq=1000)
            actions = [json.loads(record.payload) async for record in records]
            return actions, await AuditLog(store).verify(tenant.id)
        finally:
            await engine.dispose()

    return asyncio.run(read())


def test_lists_every_upstream_tool_under_its_server_name(gateway: Gateway) -> None:
    tools, _ = mcp(gateway)

    assert tools == ["fs__move_file", "fs__read_text_file", "fs__write_file", "sql__query"]


def test_an_allowed_call_reaches_the_upstream(gateway: Gateway) -> None:
    _, [result] = mcp(gateway, ("sql__query", {"sql": "SELECT * FROM analytics.orders"}))

    assert not result.is_error
    assert "42" in text(result)
    assert result.structured_content == {"result": "id | total\n1  | 42"}
    assert gateway.sql.calls == [("query", {"sql": "SELECT * FROM analytics.orders"})]


def test_a_denied_call_never_reaches_the_upstream(gateway: Gateway) -> None:
    _, [result] = mcp(gateway, ("sql__query", {"sql": "DELETE FROM analytics.orders"}))

    assert result.is_error
    assert text(result) == "Denied by policy: no policy permits this call"
    assert gateway.sql.calls == []


def test_argument_level_rules_decide_the_same_tool_both_ways(gateway: Gateway) -> None:
    _, [inside, outside] = mcp(
        gateway,
        ("fs__write_file", {"path": "/sandbox/notes.txt", "content": "hi"}),
        ("fs__write_file", {"path": "/sandbox/../etc/passwd", "content": "x"}),
    )

    assert not inside.is_error and outside.is_error
    assert gateway.fs.calls == [("write_file", {"path": "/sandbox/notes.txt", "content": "hi"})]


def held_call(gateway: Gateway) -> tuple[threading.Thread, list[types.CallToolResult]]:
    """Start a call the default policies hold for approval; it waits in its own thread."""
    results: list[types.CallToolResult] = []
    thread = threading.Thread(
        target=lambda: results.extend(mcp(gateway, ("fs__move_file", MOVE))[1]), daemon=True
    )
    thread.start()
    return thread, results


def approvals(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str, str]:
    exit_code = cli(["approvals", *args])
    out, err = capsys.readouterr()
    return exit_code, out.strip(), err.strip()


def waiting_approval(capsys: pytest.CaptureFixture[str]) -> str:
    """The id of the one held call, once `approvals list` shows it."""
    deadline = time.monotonic() + APPROVAL_TIMEOUT_S
    while time.monotonic() < deadline:
        _, listed, _ = approvals(capsys, "list")
        if listed:
            (line,) = listed.splitlines()
            assert "fs__move_file" in line and "/sandbox/b" in line
            return line.split()[0]
        time.sleep(0.05)
    raise AssertionError("no call was held")


def test_a_held_call_runs_once_an_admin_approves_it(
    gateway: Gateway, services: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    thread, results = held_call(gateway)
    approval_id = waiting_approval(capsys)
    assert gateway.fs.calls == []  # still waiting

    code, out, err = approvals(capsys, "approve", approval_id, "--by", "dana", "--yes")
    thread.join(timeout=CLIENT_TIMEOUT_S)

    assert code == 0, err
    assert out.startswith("approved")
    assert not thread.is_alive()
    [result] = results
    assert not result.is_error
    assert gateway.fs.calls == [("move_file", MOVE)]
    payloads, verification = audit_trail(services)
    assert verification.ok
    approval = next(p for p in payloads if p["action"] == "tool.call.approval")
    assert approval["details"]["decided_by"] == "dana"


def test_a_rejected_call_tells_the_client_why_and_never_runs(
    gateway: Gateway, capsys: pytest.CaptureFixture[str]
) -> None:
    thread, results = held_call(gateway)
    approval_id = waiting_approval(capsys)

    approvals(capsys, "reject", approval_id, "--by", "dana", "--note", "not today", "--yes")
    thread.join(timeout=CLIENT_TIMEOUT_S)

    [result] = results
    assert result.is_error and text(result) == "Rejected by dana: not today"
    assert gateway.fs.calls == []
    code, _, err = approvals(capsys, "approve", approval_id, "--by", "dana", "--yes")
    assert code == 1 and "already rejected" in err


def test_a_held_call_nobody_decides_expires_and_never_runs(gateway: Gateway) -> None:
    started = time.monotonic()
    _, [result] = mcp(gateway, ("fs__move_file", MOVE))

    assert result.is_error and "Nobody approved" in text(result)
    assert time.monotonic() - started >= APPROVAL_TIMEOUT_S
    assert gateway.fs.calls == []


def test_every_call_is_on_the_tenants_verified_audit_chain(
    gateway: Gateway, services: Settings
) -> None:
    mcp(
        gateway,
        ("sql__query", {"sql": "SELECT * FROM analytics.orders"}),
        ("sql__query", {"sql": "DROP TABLE analytics.orders"}),
    )

    payloads, verification = audit_trail(services)

    assert verification.ok
    assert verification.checked == 3  # decided + completed, then decided (denied)
    assert [p["action"] for p in payloads] == [
        "tool.call.decided",
        "tool.call.completed",
        "tool.call.decided",
    ]
    assert payloads[0]["details"]["decision"] == "allow"
    assert payloads[2]["details"]["decision"] == "deny"


@pytest.mark.parametrize("authorization", [None, "Bearer gw_deadbeef_" + "x" * 43])
def test_mcp_without_a_valid_key_is_a_401(gateway: Gateway, authorization: str | None) -> None:
    headers = dict(MCP_HEADERS)
    if authorization:
        headers["Authorization"] = authorization

    response = httpx.post(gateway.url, json=LIST_TOOLS, headers=headers)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_any_replica_can_answer_a_request_with_no_session_in_plain_json(gateway: Gateway) -> None:
    # Stateless JSON mode: no initialize handshake, no session id, no SSE stream.
    headers = MCP_HEADERS | {"Authorization": f"Bearer {gateway.api_key}"}

    response = httpx.post(gateway.url, json=LIST_TOOLS, headers=headers)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "mcp-session-id" not in response.headers
    assert len(response.json()["result"]["tools"]) == 4


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_mcp_only_takes_posts(gateway: Gateway, method: str) -> None:
    # A GET would open a server-to-client stream that never closes.
    headers = MCP_HEADERS | {"Authorization": f"Bearer {gateway.api_key}"}

    response = httpx.request(method, gateway.url, headers=headers, timeout=5)

    assert response.status_code == 405


def test_the_configured_upstream_timeout_is_the_one_applied(services: Settings) -> None:
    with serving(slowed(create_fake_sql_server(McpCallLog()), per_request_s=0.6)) as sql_url:
        settings = services.model_copy(
            update={"mcp_servers": {"sql": f"{sql_url}/mcp"}, "mcp_timeout_s": 1.0}
        )
        with serving(build_app(settings)) as url:
            gateway = Gateway(f"{url}/mcp", new_api_key(services), McpCallLog(), McpCallLog())
            started = time.monotonic()
            _, [result] = mcp(gateway, ("sql__query", {"sql": "SELECT * FROM analytics.orders"}))

    assert text(result) == "sql__query is unavailable."
    assert time.monotonic() - started < 5


def held_until(settings: Settings, status: str) -> str:
    """The id of the one held call, once its status is `status`."""

    async def read() -> list[tuple[str, str]]:
        engine = build_engine(settings)
        try:
            async with engine.connect() as connection:
                rows = await connection.execute(
                    select(approvals_table.c.id, approvals_table.c.status)
                )
                return [(str(row.id), row.status) for row in rows]
        finally:
            await engine.dispose()

    deadline = time.monotonic() + APPROVAL_TIMEOUT_S
    while time.monotonic() < deadline:
        if (rows := asyncio.run(read())) and rows[0][1] == status:
            return rows[0][0]
        time.sleep(0.05)
    raise AssertionError(f"no held call became {status}: {asyncio.run(read())}")


def test_a_client_that_hangs_up_abandons_its_held_call(
    gateway: Gateway, services: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    call = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "fs__move_file", "arguments": MOVE},
    }
    headers = MCP_HEADERS | {"Authorization": f"Bearer {gateway.api_key}"}
    with contextlib.suppress(httpx.TimeoutException):  # the client gives up and hangs up
        httpx.post(gateway.url, json=call, headers=headers, timeout=0.5)
    approval_id = held_until(services, "abandoned")

    code, _, err = approvals(capsys, "approve", approval_id, "--by", "dana", "--yes")

    assert code == 1 and "already abandoned" in err
    assert gateway.fs.calls == []  # nothing runs for a client that has gone


def test_an_admin_sees_the_whole_call_and_must_confirm_before_it_is_decided(
    gateway: Gateway, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    thread, results = held_call(gateway)
    approval_id = waiting_approval(capsys)
    answers = iter(["n", "y"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))

    declined = approvals(capsys, "approve", approval_id, "--by", "dana")
    assert gateway.fs.calls == []
    confirmed = approvals(capsys, "approve", approval_id, "--by", "dana")
    thread.join(timeout=CLIENT_TIMEOUT_S)

    assert declined[0] == 1 and '"destination": "/sandbox/b"' in declined[1]
    assert confirmed[0] == 0 and gateway.fs.calls == [("move_file", MOVE)]
    assert not results[0].is_error


def test_show_prints_the_whole_call(gateway: Gateway, capsys: pytest.CaptureFixture[str]) -> None:
    thread, _ = held_call(gateway)
    approval_id = waiting_approval(capsys)

    code, shown, _ = approvals(capsys, "show", approval_id)
    approvals(capsys, "reject", approval_id, "--by", "dana", "--yes")
    thread.join(timeout=CLIENT_TIMEOUT_S)

    assert code == 0
    details = json.loads(shown)
    assert details["arguments"] == MOVE
    assert (details["tool"], details["status"]) == ("fs__move_file", "pending")


def test_the_decider_defaults_to_the_admins_login(
    gateway: Gateway, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("getpass.getuser", lambda: "ops-oncall")
    thread, _ = held_call(gateway)
    approval_id = waiting_approval(capsys)

    code, out, err = approvals(capsys, "reject", approval_id, "--yes")
    thread.join(timeout=CLIENT_TIMEOUT_S)

    assert code == 0, err
    assert out.endswith("by ops-oncall")


def test_the_per_tenant_limit_comes_from_settings(services: Settings) -> None:
    with running_gateway(services, approval_max_pending_per_tenant=0) as gateway:
        _, [result] = mcp(gateway, ("fs__move_file", MOVE))

    assert result.is_error and "Too many" in text(result)
    assert gateway.fs.calls == []


def test_tool_calls_have_their_own_cap(
    services: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with running_gateway(services, max_concurrent_tool_calls=1) as gateway:
        thread, _ = held_call(gateway)  # waits for approval: holds the only place
        approval_id = waiting_approval(capsys)
        _, [busy] = mcp(gateway, ("sql__query", {"sql": "SELECT * FROM analytics.orders"}))
        approvals(capsys, "reject", approval_id, "--by", "dana", "--yes")
        thread.join(timeout=CLIENT_TIMEOUT_S)
        _, [after] = mcp(gateway, ("sql__query", {"sql": "SELECT * FROM analytics.orders"}))

    assert busy.is_error and "at capacity" in text(busy)
    assert not after.is_error
