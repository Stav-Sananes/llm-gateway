# LLM + MCP Gateway

A self-hostable gateway that puts per-tenant governance in front of LLMs and MCP tool servers.

- **`/v1/chat/completions`**: an OpenAI-compatible API in front of Anthropic and Ollama, with streaming, fallback between providers, and a semantic cache.
- **`/mcp`**: one MCP endpoint that aggregates many upstream MCP servers. Every tool call is checked against a policy and written to a tamper-evident audit log.
- **Per tenant:** API keys, rate limits, token budgets and usage accounting.
- **Every hop is traced.**

## Why not LiteLLM?

In production, LiteLLM or agentgateway is the right default. This project goes deeper on governing MCP tool calls:

1. **Argument-level policy.** Rules check the tool's arguments, not only which tool is being called: `sql.query` is allowed for `SELECT` only, `fs.write` only under `/sandbox`. Policies are written in [Cedar](https://www.cedarpolicy.com/) and evaluated inside the gateway.
2. **A tamper-evident audit log.** Each tenant gets a hash chain, and `verify` detects any edited or deleted record.
3. **Human approval.** A policy can hold a risky tool call until an admin approves it.

## Architecture

```
            ┌──────────────────────────── gateway ─────────────────────────────┐
client ───► │ auth → rate limit → budget → semantic cache → router → adapter  │ ───► Anthropic / Ollama
            │                                                                  │
MCP client ►│ auth → MCP aggregator → Cedar policy → audit chain              │ ───► upstream MCP servers
            └───────────────┬──────────────────────────────────┬───────────────┘
                            │ usage events (async)             │ traces / metrics
                         Postgres                    OpenTelemetry → Jaeger, Prometheus → Grafana
```

The domain core (routing, budgets, policy, audit) is written in a ports-and-adapters style: it never imports FastAPI, Redis or provider SDKs.

## Tech stack

| Area | Technology |
|---|---|
| API | Python 3.12, FastAPI, asyncio, httpx, Server-Sent Events (SSE) |
| Providers | Anthropic Messages API, Ollama (OpenAI-compatible) |
| MCP | MCP Python SDK (Streamable HTTP), FastMCP |
| Policy | Cedar (`cedarpy`) |
| State | Redis (rate limits, budgets), Postgres (tenants, audit, usage), Qdrant (semantic cache) |
| Observability | OpenTelemetry, Jaeger, Prometheus, Grafana |
| Delivery | Docker Compose, Helm (tested on kind), GitHub Actions |
| Quality | pytest, testcontainers, k6, ruff, mypy `--strict` |

## Roadmap

- [ ] Walking skeleton: streaming pass-through to Ollama, traced, CI
- [ ] API-key auth and tenants
- [ ] Rate limits and token budgets (reserve, then settle)
- [ ] Anthropic adapter, fallback, circuit breakers
- [ ] MCP aggregator, Cedar policy, hash-chained audit
- [ ] Usage worker and Grafana dashboard
- [ ] Semantic cache
- [ ] Mid-stream provider resume
- [ ] Human-approval hold
- [ ] Load tests (k6), chaos tests, Helm chart

## Quickstart

```bash
docker compose up -d --build     # gateway :8080, Jaeger UI :16686

# Every request needs an API key. The key is printed once; only its hash is stored.
docker compose run --rm --no-deps gateway llm-gateway admin create-tenant acme
KEY=$(docker compose run --rm --no-deps gateway llm-gateway admin create-key acme)

curl -N localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5:0.5b","stream":true,"messages":[{"role":"user","content":"Hi"}]}'

docker compose run --rm --no-deps gateway llm-gateway admin revoke-key <prefix>   # stops it at once
```

Any OpenAI SDK works too: point `base_url` at `http://localhost:8080/v1` and pass the key as `api_key`.

## Development

```bash
uv sync
uv run pytest            # unit, contract and end-to-end tests (Postgres and Redis tests need Docker)
uv run ruff check . && uv run mypy
```

## Docs

- [Spec](docs/SPEC.md): scope, decisions, resilience rules
- [Design notes](docs/design/): one per component, covering the reasoning and the trade-offs
