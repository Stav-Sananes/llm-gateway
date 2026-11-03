"""The /mcp bearer layer, in front of the MCP SDK, over an in-process ASGI transport."""

import httpx
import pytest

from llm_gateway.audit.log import AuditLog
from llm_gateway.http.mcp_endpoint import build_mcp_app, build_mcp_server
from llm_gateway.policy.model import Decision
from llm_gateway.tenancy.auth import Authenticator
from llm_gateway.tenancy.keys import issue_key
from llm_gateway.tenancy.model import Tenant
from llm_gateway.tenancy.ports import TenantStoreUnavailableError
from llm_gateway.tools.gateway import ToolGateway
from tests.support.in_memory_audit_store import InMemoryAuditStore
from tests.support.in_memory_key_cache import InMemoryKeyCache
from tests.support.in_memory_tenant_store import InMemoryTenantStore
from tests.unit.test_tool_gateway import FakeUpstreams, FixedPolicy

pytestmark = pytest.mark.anyio

LIST_TOOLS = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


class DownTenantStore(InMemoryTenantStore):
    async def tenant_for_key(self, key_hash: str) -> Tenant | None:
        raise TenantStoreUnavailableError("postgres down")


async def test_auth_with_the_tenant_store_down_is_a_503_not_a_401() -> None:
    tools = ToolGateway(
        FakeUpstreams(), FixedPolicy(Decision.ALLOW), AuditLog(InMemoryAuditStore())
    )
    app, _ = build_mcp_app(
        build_mcp_server(tools), Authenticator(DownTenantStore(), InMemoryKeyCache())
    )
    headers = MCP_HEADERS | {"Authorization": f"Bearer {issue_key().plaintext}"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://t") as client:
        response = await client.post("/", json=LIST_TOOLS, headers=headers)

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "service_unavailable"
