# 14 — Chaos runs: one dependency at a time

**Goal (SPEC §6–7):** failure behavior is documented and covered by chaos tests. The scenarios: provider killed, provider slow, Redis down, Postgres down, Qdrant down (the cache is bypassed and requests continue). For each, a stated hypothesis and its outcome.

This is build step 10b. The first version ran the five scenarios once each and all of them held. Review then found that two of them held only because the outages were short, or the traffic serial. Fixing that changed the gateway, not only the runner. Both are recorded below.

## How it runs

```
uv run python chaos/run.py [scenario ...]        (all ten by default; names as slugs: postgres-down)
  docker compose -p gwload -f compose.yaml -f chaos/compose.chaos.yaml up    (the full stack, no Ollama)
     route "smart" → [fake Claude (harness/fake_anthropic.py), fake local model (harness/fake_llm.py)]
     read timeout 2 s; breaker opens after 3 failures, tries again after 5 s
     key cache fresh for 5 s; semantic cache on, with the fake model's embeddings; rate limits lifted
  per scenario: break one dependency → probe the gateway → restore it (in a finally) → wait for recovery
  prints one Markdown row per scenario (hypothesis, observed, held); exit 1 if any failed
```

- **Three kinds of fault:**
  - `docker compose stop`: the dependency is gone. The gateway gets a fast failure: its name no longer resolves.
  - `docker compose pause`: the dependency is frozen. The kernel still accepts connections, and nothing ever answers. This is the slow failure *Release It!* warns about, worse than a refusal.
  - Replacing the container: it comes back empty (Qdrant has no volume).
- **Observed through the gateway's own outputs only:** HTTP statuses, error messages, response times, `/metrics` (circuit state, requests per provider, cache lookups), `/readyz`, and `usage_events` rows counted per tenant. No reading of internal state.
- **Blast radius:** the local proof stack (project `gwload`), shared with the load test.
  - Every fault is undone in a `finally`. SIGTERM is turned into a normal exit, so that holds when the run is killed too.
  - A scenario that errors out is reported as a row that didn't hold, and the others still run.
  - Each run leaves its tenants behind (named `chaos-*`).
- **It needs the stack to itself.** Requests per provider, cache lookups and circuit states are global counters, so other traffic on the same stack (a load run, say) can make a scenario fail, or pass, for reasons unrelated to the gateway. Usage rows are counted per tenant.

## Results

All ten held in a full run on 2026-11-05.

| Scenario | Hypothesis | Observed | Held |
|---|---|---|---|
| provider killed | Fake Claude stopped: every `smart` request is still answered (by the local model); its circuit opens at failure 3, not before; `/readyz` stays ready; once it's back, the circuit closes and it serves again. | statuses [200], 10/10 served by local; closed after 2 failures, open after 3; `/readyz` 200; after restart: serving again | yes |
| provider slow | Fake Claude frozen (accepts connections, never answers), requests one at a time: all are answered by the local model; exactly the first 3 wait out the 2 s read timeout; the circuit opens and the rest go straight to the local model. | statuses [200], 8/8 served by local, 3 of 8 took over 1.5 s (slowest 2.09 s), the rest at most 0.09 s; circuit open while frozen; closed again after unpausing | yes |
| provider slow, under load | Fake Claude frozen, 30 requests at once: all are answered; every request already in flight waits out one read timeout (2 s), none longer; then the circuit is open and new requests are fast. | statuses [200], all 30 waited out the timeout, slowest 2.30 s; circuit open; the next 5 at most 0.08 s | yes |
| Redis down | Redis stopped: the budget fails closed with 503; an unknown key is still refused (auth falls back to Postgres); each response within 1 s; answered again once Redis is back. | valid key: 503 "Usage limits are temporarily unavailable."; unknown key: 401; slowest 0.03 s | yes |
| Redis frozen | Redis paused: the same as stopped, within 1 s per request: each Redis call waits at most its 0.25 s timeout. | the same statuses; slowest 0.78 s | yes |
| Postgres down | Postgres stopped, for longer than the key cache's fresh window: a key confirmed before is still served; a key never used fails closed (503, not 401); the usage worker waits without restarting, and once Postgres is back every usage row arrives, none lost. | confirmed key: [200] ×5, the last 3 after its cache entry went stale; never-used key: 503 "Authentication is temporarily unavailable."; 5 of 5 usage rows arrived; worker restarts: 0 | yes |
| Postgres frozen | Postgres paused: a confirmed key is served without waiting on it; a never-used key gets 503 within 8 s. | confirmed key: [200], slowest 0.01 s; never-used key: 503 after 2.0 s | yes |
| Qdrant down | Qdrant stopped: the semantic cache is bypassed and every request is still answered by a model; lookups count as `unavailable`; once Qdrant is back, answers are cached and hit again. | statuses [200], 6 lookups unavailable; hits again | yes |
| Qdrant frozen | Qdrant paused: every request is still answered, none waits more than the cache's 1 s deadline; after 3 failures the cache's breaker skips Qdrant at once; once it's back, answers hit again. | statuses [200], 6 lookups unavailable; slowest 1.03 s, after the first 3 at most 0.01 s; hits again | yes |
| Qdrant replaced | Qdrant's container replaced (it comes back empty): requests are answered throughout, and the cache hits again without a gateway restart. | statuses [200]; Qdrant came back with no collections; hits again, no restart | yes |

**The checks can fail.** Three controls, each run with one protection removed:

| Control | Result |
|---|---|
| Breaker threshold raised to 100 | provider slow: all 8 waited out the timeout → **no** |
| Stale keys not served (`GATEWAY_KEY_CACHE_STALE_S` equal to the fresh window) | Postgres down: [200, 200, 503, 503, 503], the 503s once the entry went stale → **no** |
| The collection never re-created | Qdrant replaced: caught by an integration test with the collection deleted under the store; before the fix, review saw the cache fail for good until a restart. |

## What the first runs hid, and what changed

| Found | Why the first runs missed it | Fix |
|---|---|---|
| **A Postgres outage became a full chat outage after 60 s.** Cache entries were written on a miss and expired after 60 s. After that, every key needed Postgres, and every request got 503. | Each outage lasted a few seconds, inside the 60 s. | Each entry records when its key was last confirmed. It is fresh for `GATEWAY_KEY_CACHE_TTL_S` (60 s) and re-checked after that, but Redis keeps it for `GATEWAY_KEY_CACHE_STALE_S` (1 h). While Postgres is unavailable, a stale entry is still accepted. A re-check that finds the key revoked marks it revoked. A refresh never overwrites a revoke mark (a Lua compare-and-set), so a re-check racing a revoke can't bring the key back. The scenario now outlasts the fresh window. |
| **A frozen provider holds every request in flight for a full timeout**, not just the first 3: the breaker counts a failure only when a call ends. With the defaults (60 s read timeout, per-provider bulkhead 1,000, chat cap 1,024), one frozen provider could hold almost every chat place for a minute. And a provider sending keep-alive pings never trips a per-read timeout at all. | The scenario sent requests one at a time. | A **first-chunk deadline** (`GATEWAY_UPSTREAM_FIRST_CHUNK_TIMEOUT_S`, 20 s): a provider that hasn't started answering by then has failed, whatever bytes it sends. The **per-provider bulkhead** drops to 512, half the chat cap, so a frozen provider leaves the other half for the fallback. A scenario now freezes the provider under 30 concurrent requests. |
| **Only the provider was ever frozen.** Redis, Postgres and Qdrant were only stopped, which fails fast, so their timeouts were never exercised: removing Redis's socket timeouts passed every scenario. | — | Frozen variants of all three. |
| **A Qdrant that came back empty disabled the cache until the gateway restarted.** The store created its collection once and remembered that. | `compose stop`/`start` keeps the container's files. | A 404 from Qdrant makes the store create its collection again on the next call. A scenario replaces the container. |
| **Several checks could pass without the mechanism working.** Provider slow passed with 0 slow requests ("at most 3"), and had no upper bound, so SDK retries or a 10× timeout passed too. The circuit read "closed" right after a success that always closes it. A missing gauge read as 0, "closed". | Review mutated the gateway in a copy: pytest caught 17 of 18 mutations, the runner 11. | Provider slow requires exactly 3 slow requests, each under one timeout plus 1 s, the circuit open while frozen, and all 8 answered locally. The vacuous check is gone, and a missing gauge is an error. A unit test checks the Redis verdict rejects each way the gateway could get it wrong. The mutation both missed (the usage worker crashing, not waiting, when Postgres is down: Docker restarted it, and no row was lost) now fails a unit test and the Postgres scenario (`restarts == 0`). |

## What the runs say about the design

| Behavior | Why it is this way | Where it was decided |
|---|---|---|
| **Redis down stops chat with 503, except for semantic-cache hits** (served before the budget is checked). | Spend is money. Without Redis there is no budget counter, and no usage stream either (the events queue in Redis), so failing open would mean spend that is neither capped nor billed. The cost: Redis is a single point of failure for chat while budgets are on. The answer is a highly available Redis (a replica with failover), not failing open. | Step 3 (`04-rate-limits-and-budgets.md`) |
| **An unknown key is refused (401) with Redis down; a key never confirmed gets 503 with Postgres down.** | Postgres is the source of truth for keys, Redis only caches them. With Postgres down, "not in the cache" can't be told apart from "doesn't exist", so the answer is "not now", never "invalid". | Step 2 (`03-bearer-auth.md`) |
| **A frozen provider costs one timeout per request in flight, then none.** | Timeouts bound each wait (2 s read here; 60 s per read and 20 s to the first chunk by default), and the breaker turns repeated slow failures into fast ones. | Step 4 (`05-anthropic-fallback-breakers.md`), and the first-chunk deadline above |
| **`/readyz` stays ready while one provider's circuit is open.** It fails only when every circuit is open. | One provider down is what fallback routing is for. | Step 4 |
| **Usage rows survive a Postgres outage.** | They queue in Redis (a stream with a consumer group); the worker retries and acknowledges only after the insert. | Step 6 (`09-usage-and-metrics.md`) |

## Known gaps

- **No breaker on Redis.** A frozen Redis costs every request three 0.25 s timeouts in a row (key cache, rate limit, budget): 0.78 s each before its 503. That's bounded, but a breaker would make it fast after the first few.
- **A Redis outage moves all auth onto Postgres,** uncached, through a pool of 10. Under load, that becomes a Postgres load spike, which the runner, sending one request at a time, can't see.
- **A stale key works through a Postgres outage only if it was confirmed within the last hour,** and a key revoked while its cache mark was also lost keeps working until Postgres is back or that hour runs out.
- **After the first chunk there is no total deadline.** A provider that trickles a token every 59 s keeps its call, and its bulkhead slot, alive.
- **One dependency at a time.** No combined failures, and no network faults short of stop and pause: added latency, packet loss and resets mid-stream would need a proxy such as Toxiproxy. The providers' own failure modes (mid-stream errors, garbage, slow tokens) are covered by integration tests instead.
- **Chat only.** MCP tool calls aren't exercised. The behavior they'd show is designed but not chaos-tested: with Postgres down, audit fails closed, so tool calls are refused.
- **Failures start between requests,** never in the middle of one, except for the provider under load. A provider dying mid-stream is covered by mid-stream resume (step 8); Redis or Postgres dying mid-stream isn't.
- **The gateway and the usage worker aren't killed.** Redelivery after a worker crash is covered by an integration test (step 6), and the gateway's shutdown by step 9's tests.
- **Short timeouts make it fast.** The chaos stack uses a 2 s read timeout, a 5 s breaker reset and a 5 s key-cache window. The defaults are 60 s, 30 s and 60 s: the same shape, more slowly.

## Interview questions to be ready for

1. *Redis goes down. What does a tenant see, and why is that the right answer?*
2. *Why is a frozen provider worse than a dead one, and what limits the damage when many requests are in flight?*
3. *Postgres is down for ten minutes. Which requests still work, and is any usage lost?*
