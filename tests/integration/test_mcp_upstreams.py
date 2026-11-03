"""The MCP upstream adapter, talking real MCP over Streamable HTTP to fake servers."""

import time
from collections.abc import Iterator

import pytest

from harness.fake_mcp import McpCallLog, create_fake_fs_server, create_fake_sql_server
from llm_gateway.providers.mcp_upstreams import HttpMcpUpstreams
from llm_gateway.tools.model import UnknownToolError, UpstreamUnavailableError
from tests.support.servers import serving, silent_local_url, unused_local_url

pytestmark = pytest.mark.anyio


@pytest.fixture
def sql_log() -> McpCallLog:
    return McpCallLog()


@pytest.fixture
def sql_url(sql_log: McpCallLog) -> Iterator[str]:
    with serving(create_fake_sql_server(sql_log)) as url:
        yield f"{url}/mcp"


@pytest.fixture
def fs_url() -> Iterator[str]:
    with serving(create_fake_fs_server(McpCallLog())) as url:
        yield f"{url}/mcp"


async def test_lists_every_servers_tools(sql_url: str, fs_url: str) -> None:
    upstreams = HttpMcpUpstreams({"sql": sql_url, "fs": fs_url}, timeout_s=5)

    names = sorted(spec.qualified_name for spec in await upstreams.list_tools())

    assert names == ["fs__move_file", "fs__read_text_file", "fs__write_file", "sql__query"]


async def test_an_unreachable_server_is_left_out_of_the_list(sql_url: str) -> None:
    upstreams = HttpMcpUpstreams({"sql": sql_url, "gone": unused_local_url()}, timeout_s=5)

    assert [spec.qualified_name for spec in await upstreams.list_tools()] == ["sql__query"]


async def test_a_call_reaches_the_server_and_comes_back_as_plain_data(
    sql_url: str, sql_log: McpCallLog
) -> None:
    upstreams = HttpMcpUpstreams({"sql": sql_url}, timeout_s=5)

    result = await upstreams.call("sql", "query", {"sql": "SELECT 1"})

    assert sql_log.calls == [("query", {"sql": "SELECT 1"})]
    assert not result.is_error
    assert result.content[0] == {"type": "text", "text": "id | total\n1  | 42"}


async def test_a_tool_failing_on_the_server_is_an_error_result_not_an_outage(fs_url: str) -> None:
    upstreams = HttpMcpUpstreams({"fs": fs_url}, timeout_s=5)

    result = await upstreams.call("fs", "read_text_file", {"path": "/nope"})

    assert result.is_error


async def test_an_unknown_server_is_an_unknown_tool() -> None:
    with pytest.raises(UnknownToolError):
        await HttpMcpUpstreams({}, timeout_s=5).call("nope", "x", {})


async def test_an_unreachable_server_is_unavailable() -> None:
    upstreams = HttpMcpUpstreams({"gone": unused_local_url()}, timeout_s=5)

    with pytest.raises(UpstreamUnavailableError):
        await upstreams.call("gone", "x", {})


async def test_a_hung_server_is_given_up_on_at_the_deadline() -> None:
    started = time.monotonic()
    with silent_local_url() as url:
        upstreams = HttpMcpUpstreams({"hung": url}, timeout_s=0.5)
        with pytest.raises(UpstreamUnavailableError):
            await upstreams.call("hung", "x", {})

    assert time.monotonic() - started < 3.0


@pytest.mark.parametrize("name", ["a__b", ""])
def test_server_names_that_would_break_tool_names_are_refused(name: str) -> None:
    with pytest.raises(ValueError, match="must be non-empty"):
        HttpMcpUpstreams({name: "http://x"}, timeout_s=5)
