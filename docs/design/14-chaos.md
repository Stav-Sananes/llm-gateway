# 14 — Chaos runs: one dependency at a time

**Goal (SPEC §6–7):** failure behavior is documented and covered by chaos tests. The scenarios: provider killed, provider slow, Redis down, Postgres down, Qdrant down (the cache is bypassed and requests continue). For each, a stated hypothesis and its outcome.

This is build step 10b.

## How it runs

```
uv run python chaos/run.py [scenario ...]        (all five by default)
  docker compose -p gwload -f compose.yaml -f chaos/compose.chaos.yaml up    (the full stack)
     route "smart" → [fake Claude (harness/fake_anthropic.py), fake local model (harness/fake_llm.py)]
     read timeout 2 s, breaker opens after 3 failures and tries again after 5 s
     semantic cache on, with the fake model's embeddings; tenant rate limits lifted
  per scenario: break one dependency → probe the gateway → restore it → wait for recovery
  prints one Markdown row per scenario (hypothesis, observed, held); exit 1 if any failed
```

- **Injection:** `docker compose stop` for a dependency that's gone (connections refused), `docker compose pause` for one that's frozen: the kernel still accepts its connections, and nothing ever answers. That is the slow failure *Release It!* warns about, worse than a refusal.
- **Observed through the gateway's own outputs only:** HTTP statuses, the error message, response times, `/metrics` (circuit state, requests per provider, cache lookups), `/readyz`, and the `usage_events` rows. No reading of internal state.
- **Blast radius:** the local proof stack (project `gwload`), shared with the load test. Every scenario restores what it broke, in a `finally`, and waits until the gateway answers normally before the next one starts.

## Results

Run twice in full with the final checks (2026-11-05), the same outcome both times:

| Scenario | Hypothesis | Observed | Held |
|---|---|---|---|
| provider killed | Fake Claude stopped: every `smart` request is still answered (by the local model); after 3 failures its circuit opens; `/readyz` stays ready; once it's back, the circuit closes and it serves again. | statuses [200], 10/10 served by local, circuit open, `/readyz` 200; after restart: serving again, circuit closed | yes |
| provider slow | Fake Claude frozen (accepts connections, never answers): requests are still answered; only the first 3 wait out the 2 s read timeout, then the circuit opens and the rest go straight to the local model. | statuses [200], 3 of 8 took over 1.5 s, the rest at most 0.08 s; circuit closed again after unpausing | yes |
| Redis down | Redis stopped: auth falls back to Postgres (a valid key still authenticates, an unknown one is still refused); the budget fails closed with 503; nothing hangs (each response within 1 s); with Redis back, requests are answered again. | valid key: 503 "Usage limits are temporarily unavailable.", slowest 0.01 s; unknown key: 401; answered again once Redis was back | yes |
| Postgres down | Postgres stopped: a key used before is still served from the Redis key cache; a key never used fails closed (503, not 401); the usage worker waits, and once Postgres is back every usage row arrives, none lost. | cached key: [200]; never-used key: 503 "Authentication is temporarily unavailable."; 5 of 5 usage rows arrived after Postgres came back; usage worker restarts: 0 | yes |
| Qdrant down | Qdrant stopped: the semantic cache is bypassed and every request is still answered by a model; lookups count as `unavailable`; once Qdrant is back, answers are cached and hit again. | statuses [200], 6 lookups unavailable; hits again once Qdrant was back | yes |

**The checks can fail.** As a control, the provider-slow scenario was run with the breaker's threshold raised to 100: all 8 requests waited out the 2 s timeout, the runner reported **no**, and exited 1.

## What the runs say about the design

| Behavior | Why it is this way | Where it was decided |
|---|---|---|
| **Redis down stops every chat request with 503.** The budget can't be checked, so nothing is spent. | Spend is money; failing open would turn an outage into an unbounded bill. The cost: Redis is a single point of failure for chat while budgets are on. | Step 3 (`04-rate-limits-and-budgets.md`) |
| **An unknown key is refused (401) with Redis down, and a never-seen key gets 503 with Postgres down.** | Postgres is the source of truth for keys, Redis only caches them. With Postgres down, "not in the cache" can't be told apart from "doesn't exist", so the answer is "not now", never "invalid". | Step 2 (`03-bearer-auth.md`) |
| **A frozen provider costs 3 slow requests, then none.** | The read timeout (2 s here, 60 s by default) bounds each wait; the breaker turns repeated slow failures into fast ones. With the default 60 s timeout, 5 requests wait a minute each before the circuit opens. | Step 4 (`05-anthropic-fallback-breakers.md`) |
| **`/readyz` stays ready while one provider's circuit is open.** It fails only when every circuit is open. | One provider down is what fallback routing is for. | Step 4 |
| **Usage rows survive a Postgres outage.** | They queue in Redis (a stream with a consumer group); the worker retries and acknowledges only after the insert. | Step 6 (`09-usage-and-metrics.md`) |

## Known gaps

- **One dependency at a time.** No combined failures (Redis and Postgres both down; a provider dying while Qdrant is down).
- **No network faults short of stop/pause.** Added latency, packet loss, a connection reset mid-stream and DNS failure would need a proxy such as Toxiproxy between the gateway and each dependency. The fake providers' own failure modes (mid-stream errors, garbage, slow tokens) are covered by integration tests instead.
- **Chat only.** MCP tool calls aren't exercised. The behavior they'd show is designed but not chaos-tested: with Postgres down, audit fails closed, so tool calls are refused.
- **Failures start between requests, never during one.** A dependency that dies mid-stream is covered for providers (mid-stream resume, step 8), not for Redis or Postgres.
- **Not killed: the gateway itself or the usage worker.** Redelivery after a worker crash is covered by an integration test (step 6), and the gateway's shutdown by step 9's tests.
- **Short timeouts make it fast.** The chaos stack uses a 2 s read timeout and a 5 s breaker reset; the defaults (60 s, 30 s) give the same shape, more slowly.

## Interview questions to be ready for

1. *Redis goes down. What does a tenant see, and why is that the right answer?*
2. *Why is a frozen provider worse than a dead one, and what limits the damage?*
3. *Postgres is down for a minute. Which requests still work, and is any usage lost?*
