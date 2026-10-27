# LLM + MCP Gateway — Spec

Status: agreed 2026-10-27 (grilling session). Changes to this file go through a PR like code.

## 1. What it is

A self-hostable gateway with two front doors:

- **`/v1/chat/completions`**: an OpenAI-compatible API in front of Anthropic (hosted) and Ollama (self-hosted).
- **`/mcp`**: one MCP endpoint (Streamable HTTP) that aggregates N upstream MCP servers.

Every request belongs to a tenant. The gateway enforces rate limits and token budgets, caches answers semantically, falls back between providers (even mid-stream), and puts every MCP tool call through an argument-level policy check before writing it to a tamper-evident audit log.

## 2. Why build it when LiteLLM exists

In production I would use LiteLLM or agentgateway. They already do model routing and per-tool MCP permissions. I built this project to own the hard parts, and it goes past them in three places:

1. **Argument-level policy as code.** Rules look at the *arguments* of a tool call, not only the tool's name: `sql.query` is allowed only for `SELECT` on allowed schemas, `fs.write` only under `/sandbox`. Policies are Cedar and are evaluated in-process.
2. **A tamper-evident audit log.** Each tenant has a hash chain: `hash_n = H(hash_{n-1} || canonical(event_n))`. A `verify` command finds any edited or deleted row.
3. **Human approval.** A policy can answer `require_approval`. The call is held until an admin approves or rejects it, or until it times out.

## 3. Audience and constraints

- It has to convince AI platform/infra roles first and AI/LLM engineer roles second.
- It runs locally with `docker compose up`. The Helm chart is tested on kind. There's no always-on cloud deployment.
- Learning comes before speed. The target is about 8 weeks, and it's flexible.

## 4. Request flows

### 4.1 Chat completions

```
client ──► auth ──► rate limit ──► budget reserve ──► semantic cache ──► router ──► provider adapter
                                                          │ hit                        │
                                                          ▼                            ▼
client ◄──────────────────────── SSE stream (OpenAI chunk format) ◄────────── domain chunk events
                                                                                       │
                                     budget settle + usage event (async) ◄─────────────┘
```

- The gateway **always streams from the upstream provider**. A client request without `stream: true` gets the chunks aggregated into one `chat.completion` response. This keeps metering, caching and resume on a single code path.
- Provider adapters turn upstream wire formats into domain chunk events, and the HTTP presenter renders those events as OpenAI chunks. No provider format leaks past its adapter.

### 4.2 MCP tool call

```
client ──► auth ──► /mcp (aggregator) ──► resolve namespaced tool (server__tool)
       ──► Cedar policy(tenant, tool, arguments) ──► allow | deny | require_approval
       ──► audit append (sync, hash-chained) ──► upstream MCP server ──► audit result ──► client
```

## 5. Components and decisions

| Area | Decision |
|---|---|
| Language | Python 3.12, FastAPI, asyncio, httpx. Tooling: uv, ruff, mypy `--strict`, pytest. |
| Architecture | Ports and adapters. The domain core (routing, budget, policy, audit chain) never imports FastAPI, Redis, SQLAlchemy or provider SDKs. `main` is the only composition root. |
| Auth | Bearer API key, mapped to a tenant. Keys are stored as SHA-256 hashes in Postgres and cached in Redis. OAuth 2.1 for MCP is on the roadmap. |
| Rate limit | Redis token bucket per tenant (Lua, atomic). **Fails open** to a local in-memory bucket when Redis is down. |
| Budget | Tokens per tenant per period. **Reserve then settle**: an atomic reserve of `prompt_estimate + max_tokens`, then settle the actual usage when the stream ends and refund the difference. **Fails closed** when Redis is down. |
| Providers | Anthropic (translation from OpenAI to the Messages API, including streaming and tool calls) and Ollama (OpenAI-compatible endpoint). |
| Fallback | A circuit breaker per provider. Fall back on connect errors, 5xx, or a timeout before the first token. |
| Mid-stream resume | If a provider dies after streaming has started, the fallback provider continues from the partial output via assistant prefill. **Invisible to the client** on the wire. The switch is **recorded internally**: audit, OTel span, and one usage row per provider. |
| Semantic cache | Qdrant with Ollama embeddings. Strict rules: isolated per tenant, used only when the request has no tools and `temperature <= 0.3`, cosine similarity ≥ 0.95 (configurable), TTL, and an opt-out header. Bypassed whenever the response contains tool calls. |
| MCP | The gateway is an MCP server (Streamable HTTP) that aggregates upstream MCP servers. Tool names are namespaced `server__tool`. |
| Policy | Cedar via `cedarpy`, evaluated in-process. Decision: allow, deny or require_approval. |
| Audit | A hash chain per tenant in Postgres, appended **synchronously** on the tool-call path. Redaction rules run before hashing. Includes a `verify` CLI. |
| Approval hold | The held call is parked in Postgres and approved or rejected through the CLI or API. The client waits, up to a timeout. |
| Usage | Usage events go through an async worker into Postgres. |
| Observability | OTel traces on every hop (GenAI semantic conventions), Prometheus metrics, one Grafana dashboard. |
| Demo MCP servers | Our own FastMCP SQL server (`SELECT` only, allowed schemas) and the official filesystem server (writes only under `/sandbox`). |
| Out of scope | vLLM, Terraform, admin UI, non-chat endpoints. |

## 6. Resilience rules

- Every outbound call has explicit connect and read timeouts. No unbounded queues.
- There's one circuit breaker per provider, and its state shows up in metrics and `/readyz`.
- Failure behavior is documented and covered by chaos tests: provider killed, provider slow, Redis down, Postgres down, and Qdrant down (the cache is bypassed and requests continue).

## 7. Proof

- **Load:** k6 against our fake LLM, which has fixed latency. Report the latency the gateway adds at p50 and p99 for a given RPS.
- **Chaos:** the scenarios in section 6, each with a stated hypothesis and its outcome.
- **Demo:** `docker compose up`, a Helm chart tested on kind, and a 2-minute video.

## 8. Build order

1. Walking skeleton: Compose, streaming pass-through to Ollama, traced end to end, CI green
2. Auth and tenants
3. Rate limit and budget
4. Anthropic adapter, fallback and circuit breaker
5. MCP aggregator, Cedar policy and audit chain
6. Usage worker and Grafana
7. Semantic cache
8. Mid-stream resume
9. Approval hold
10. k6, chaos runs, Helm and the video

## 9. Working agreement

- Claude writes the code and Stav reviews it. Stav can take over any piece at any time.
- One component per PR, about 300 lines or fewer, with a design note in `docs/design/` (why, trade-offs, rejected alternatives) and 2–3 interview questions.
- Merge only after Stav explains the PR back in their own words.
- Integration tests use real Redis, Postgres and Qdrant (testcontainers), plus fakes we own: the fake LLM and the fake MCP server. Mock only types we own.

## 10. Known interview pressure points

- **Silent mid-stream resume:** one answer can come from two models without the client knowing. Defense: it's recorded internally, and it's a deliberate availability-first choice. Be ready to argue the opposite position too.
- **Semantic cache correctness:** false hits. Defense: strict eligibility rules and a high threshold, and the hit rate and threshold show up on the dashboard.
- **The answer to "Why not LiteLLM?"** is in section 2.
