# 08 — The MCP aggregator: one governed endpoint for many MCP servers

**Goal:** an MCP client connects once, to `/mcp` on the gateway, and sees the tools of every upstream MCP server. Every tool call is authenticated, judged by the Cedar policies (07) on its arguments, written to the tenant's audit chain (06), and only then forwarded.

This completes step 5 (06 → 07 → 08).

## How a tool call flows

```
MCP client ──POST /mcp (Streamable HTTP, Bearer gw_…)──► _BearerAuth (http/mcp_endpoint.py)
                                                          401 / 503 before the MCP SDK sees it
   ─► MCP Server (SDK, stateless, JSON responses) ─► on_call_tool ─► ToolGateway (tools/gateway.py)
         split "sql__query" → server "sql", tool "query"        (not server__tool → error result)
         CedarPolicyEngine.decide(ToolCall)                     ALLOW / DENY / REQUIRE_APPROVAL
         AuditLog.record("tool.call.decided", tool, arguments, decision, reasons)   ← before the call
           DENY              → error result "Denied by policy: <ids>"
           REQUIRE_APPROVAL  → error result (the approval hold is step 9)
           ALLOW             → HttpMcpUpstreams.call ─► upstream MCP server
                                 AuditLog.record("tool.call.completed", is_error, result size + SHA-256)
                                 or "tool.call.failed" → error result "<tool> is unavailable."
```

`tools/` (the use case) imports no MCP SDK; `providers/mcp_upstreams.py` and `http/mcp_endpoint.py` are the only places that do.

## Decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| **Tools are namespaced `server__tool`**; server names may not contain `__`. | Two servers can both have a `query` tool; the name must say which one, and policies key on the server. | Merging by name: collisions decide which server runs. |
| **Audit the decision before forwarding**, synchronously; an audit failure stops the call. | "Every tool call is audited" must hold even when the gateway crashes mid-call. An unaudited call is worse than a refused one. | Auditing after the call, or asynchronously: a crash loses the record of a call that ran. |
| **Audit the result's size and SHA-256, not the result.** | Results can be large or sensitive (query rows). The digest still proves later what was returned, if the result is kept elsewhere. | Storing results in the chain: the chain becomes a data store full of customer data. |
| **Denials are tool results with `isError`**, not protocol errors. | MCP clients (and the models driving them) read the reason and can adapt; a protocol error looks like a broken server. | JSON-RPC errors. |
| **Stateless Streamable HTTP with JSON responses.** | Any replica can serve any request; no sticky sessions, no session store. | Stateful sessions: a replica restart drops every client. |
| **Bearer auth in an ASGI layer in front of the SDK**, same keys and cache as the chat API. | One identity for both front doors; unauthenticated requests never reach MCP parsing. | Per-handler auth: easy to forget on a new method. |
| **One short-lived upstream session per operation**, bounded by one deadline (`GATEWAY_MCP_TIMEOUT_S`, 30 s; connect 2 s). Unreachable servers are left out of `tools/list`. | Nothing to reconnect after an upstream restart; a hung server costs at most the deadline (Release It: Timeouts). One dead server must not hide every other server's tools. | Long-lived sessions: reconnect logic, and a stuck session blocks every call. |

## Configuration

```bash
GATEWAY_MCP_SERVERS='{"sql": "http://sql-mcp:8000/mcp", "fs": "http://fs-mcp:8000/mcp"}'
GATEWAY_MCP_TIMEOUT_S=30
GATEWAY_POLICY_FILE=/etc/gateway/policies.cedar   # unset: the packaged demo policies
```

## Known gaps (on purpose)

- **No rate limit or budget on `/mcp` yet**; tool calls cost no tokens, but a tenant can still flood upstreams.
- **The tool list is fetched per request** (one session per server); a cache with a short TTL would cut that.
- **`tools/list` shows every tool**, including ones the tenant's policy would always deny; filtering needs policies that can be evaluated without arguments.
- **Only tools**: resources, prompts and sampling from upstream servers aren't aggregated.
- **Demo servers** (the SQL server in a read-only transaction, the filesystem server confined to `/sandbox`) and their Compose wiring come with the demo in step 10; tests use fake servers in `harness/fake_mcp.py`.

## Interview questions to be ready for

1. *Why write the audit record before the tool call instead of after?*
2. *Why is a policy denial a successful MCP response with `isError`, not an error?*
3. *One upstream MCP server hangs. What does a client calling another server's tool see?*
