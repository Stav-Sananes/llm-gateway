# 05 — Anthropic adapter, fallback routing and circuit breakers

**Goal:** the gateway can serve a model name from Anthropic or Ollama, falls back to the next provider when one is down, and stops hammering a provider that keeps failing.

## How a request is routed

```
ChatCompletions ─► Router (routing/router.py)
                     model in GATEWAY_ROUTES?  yes → its targets, in order
                                               no  → default provider, model name unchanged
                     for each target:
                       GuardedProvider (routing/breaker.py): circuit open? → skip
                         └─ provider adapter: AnthropicProvider | OpenAICompatibleProvider
                       unavailable before the first chunk → next target
                       first chunk arrived → this target's answer stands
                     every target unavailable → 502
```

`routing/` and `chat/` import no SDK. Only `providers/anthropic.py` knows the Messages API.

## Decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| **The official `anthropic` SDK (2.4)** for the adapter, with `max_retries=0` and the gateway's own timeouts (connect 2 s, read 60 s between events). | The SDK owns the wire format and error types. Its retries would add latency in front of the router, which already falls back. | Raw httpx against the API: re-implements parsing the SDK maintains. SDK retries on: a dead provider costs 3 tries before fallback. |
| **Translate at the edge:** system/developer messages → top-level `system`; text and image parts → content blocks; stop reasons → OpenAI finish reasons (`refusal` → `content_filter`); prompt tokens = input + cache creation + cache read. | Clients keep speaking OpenAI; nothing Anthropic-shaped leaks past the adapter. | Passing Anthropic events through: every client would need two formats. |
| **`temperature` is dropped for Anthropic.** | The API version the SDK sends (`2026-10-21`) rejects sampling parameters on every model. | Forwarding it: every request with a temperature would 400. |
| **Refusal fallbacks on by default** for the models that take them (`fallbacks: "default"`, beta `server-side-fallback-2026-07-01`): Opus 5.5, Opus 5, Fable 5.1, Sonnet 5.5. `GATEWAY_ANTHROPIC_REFUSAL_FALLBACKS=false` turns it off. | A policy decline is re-run by Anthropic on a suitable model in the same call, instead of the client getting nothing. | No fallback: a false-positive refusal ends the request. |
| **Fallback only before the first chunk**, on "try elsewhere" errors: connect failure, timeout, 5xx, 529, 429, 401/403, an open circuit, or an `error` event before any text. Bad requests (400, 404) are not retried elsewhere. | Before the first byte the client can't tell; after it, switching providers changes the answer mid-sentence (step 8 handles that deliberately). A 400 would fail the same way everywhere. | Falling back on everything: a malformed request is sent to every provider. |
| **Routes are explicit** (`GATEWAY_ROUTES`, JSON: model name → ordered targets). Unrouted names go to the default provider unchanged. | Fallback needs a mapping between model names that mean "the same job" on different providers; only the operator knows it. Unrouted names keep step 1's behaviour. | Guessing by prefix (`claude-*` → Anthropic): no fallback model, and surprising. |
| **One circuit breaker per provider**: opens after 5 failures in a row, half-opens after 30 s, one trial at a time. A provider counts as healthy once it streams its first chunk; a 4xx is not a failure. | Stops sending traffic (and timeouts) to a provider that is down, so fallback is instant (Release It: Circuit Breaker). | Counting rejected requests: one client's bad requests would open the circuit for everyone. |
| **`/readyz` shows every circuit; 503 only when all are open.** | Operators see which provider is out; a load balancer drains a replica only when it truly can't serve. | 503 when any circuit is open: one provider's outage would take every replica out of rotation. |

## Configuration

```bash
GATEWAY_ANTHROPIC_API_KEY=sk-ant-...
GATEWAY_ROUTES='{"smart": [{"provider": "anthropic", "model": "claude-opus-5-5"},
                           {"provider": "ollama",    "model": "qwen2.5:0.5b"}]}'
GATEWAY_BREAKER_FAILURE_THRESHOLD=5
GATEWAY_BREAKER_RESET_S=30
```

## Known gaps (on purpose)

- **No tool calls through Anthropic yet.** Requests with `tool` messages or assistant `tool_calls` get a 400 from the adapter; the domain chunk has no tool-call field yet.
- **Thinking is not streamed to clients**, but its tokens are in `completion_tokens` and charged to the budget.
- **Breaker state is per replica** and only visible in `/readyz`; Prometheus metrics come in step 6.
- **A refused request that Anthropic re-ran on another model** is reported under the requested model name; the served model isn't surfaced yet.

## Interview questions to be ready for

1. *Why fall back only before the first chunk?*
2. *Why doesn't a 400 from Anthropic open the circuit?*
3. *Why turn off the SDK's retries when retrying is good practice?*
