# 13 — Load test: what the gateway adds, and how it behaves past capacity

**Goal (SPEC §7):** k6 against our fake LLM, which has a fixed latency. Report the latency the gateway adds, at p50 and p99, for a given RPS.

This is build step 10a. The load run found a real defect: an overloaded gateway opened its healthy provider's circuit. Fixing it took two attempts, and review rejected the first one. Both are recorded below.

## How it's measured

```
load/run.sh [RPS] [DURATION]
  docker compose -f compose.yaml -f load/compose.load.yaml up        (the full stack)
     fake-llm: 20 tokens, 10 ms apart, about 230 ms per answer     (harness/Dockerfile)
       sends its headers with the first token, as the gateway does
     gateway → fake-llm, tenant limits lifted, cache off; tracing on, as in compose
  k6 (grafana/k6, on the compose network), constant arrival rate, streamed requests:
     round 1: fake LLM directly     round 1: through the gateway
     round 2: fake LLM directly     round 2: through the gateway      (alternating: same drift)
  each request checks status 200 and a final [DONE]; under 99 % passing, k6 fails the run
```

- **Two measures:** the whole streamed answer (`http_req_duration`) and the first byte (`http_req_waiting`). Both paths send their headers with the first token, so the first byte *is* the first token on both.
- **Percentiles cover served (2xx) answers only.** A fast 503 from shedding mustn't flatter them. The share refused is reported next to them.
- **Open model:** a constant arrival rate, so a slow gateway doesn't slow down its own load (no coordinated omission). "Dropped" counts arrivals k6 had no free virtual user for.

Machine: Apple M4 laptop, Docker Desktop with 10 CPUs and 8 GiB. One gateway process (uvicorn, one worker). The whole stack runs on the same machine as k6.

## What the gateway adds (50 requests/s, two 30 s rounds each)

Every request returned 200 and finished with `[DONE]`.

| | whole answer p50 | p99 | first token p50 | p99 |
|---|---|---|---|---|
| fake LLM directly (round 1 / 2) | 234 / 229 ms | 258 / 258 ms | 12.2 / 11.7 ms | 17.4 / 17.2 ms |
| through the gateway (round 1 / 2) | 229 / 227 ms | 276 / 287 ms | 14.1 / 13.9 ms | 31.3 / 28.6 ms |

- **Median first token:** 12 ms directly, 14 ms through the gateway, so about 2 ms added. That covers auth, the rate limit, the budget reservation, the cache lookup decision and the extra hop.
- **p99 first token:** 17 ms becomes 29–31 ms. Percentiles don't subtract, so this compares the two distributions; it isn't the gateway's own p99.
- **Whole answer:** no difference beyond run-to-run noise (the gateway's median was even slightly lower). Tokens are passed on as they arrive.

## Past capacity

Every cell is a 20 s run. Most were repeated, and both runs are shown where they were.

**Short answers (230 ms).** One process serves about **300–320 answers a second** of this size.

| rate | before the fix (no limit) | first fix: cap of 64 in flight | **final: shed on event-loop lag > 50 ms** |
|---|---|---|---|
| 200 rps | all served, p99 366 ms | 2 % refused, p99 355 ms | all served, p99 328 / 349 ms (lag threshold 100 ms) |
| 300 rps | — | 14 % refused, p99 335 ms | **all served, p99 379 / 481 ms** |
| 400 rps | **88 % failed (502), circuit opened**, p99 10.5 s | 36 % refused, p99 368 ms | **21 % refused (≈ 315 rps served), p99 2.4 / 2.8 s, circuit closed** |

**Long answers (50 tokens, 40 ms apart: 2 s each):**

| rate | in flight | final |
|---|---|---|
| 100 rps | ~200 | all served, p99 2.19 s, nothing refused |
| 150 rps | ~300 | all served, p99 2.36 s, nothing refused |

The first fix would have refused most of these: 64 streams allow only about 32 answers a second of this length.

Other lag thresholds, measured at 300 and 400 rps:
- **30 ms:** 13 % refused even at 300 rps; at 400 rps, p99 1.1–1.5 s.
- **100 ms:** everything served at 300 rps; at 400 rps, p99 3–7 s.

The threshold trades tail latency under overload against refusing early. The default is 50 ms. The gauge `gateway_event_loop_lag_seconds` is exported, so it can be tuned per deployment.

## The defect, and the two fixes

**The defect.** At 400 rps the single event loop fell behind:
- Redis calls hit their 250 ms timeouts.
- Waits for one of the provider's 100 connections timed out.

The adapter reported those pool timeouts as the provider timing out, the circuit breaker counted them, and the circuit opened. From then on every request got a 502, although the fake LLM was idle (directly, it served 400 rps at p99 242 ms). A local overload had become a full outage, the chain reaction *Release It!* warns about.

**First fix, rejected in review.** A middleware capped requests in flight at 64, before anything else ran. Review found two problems:
- **Denial of service.** It took a place before the body was read or the key checked. 70 connections that sent headers and one byte of body, without a key, held every place: every tenant got 503 (reproduced).
- **The wrong measure.** In flight = rate × answer time. With 230 ms answers, 64 happens to sit at the knee. With real answers of 5–30 s it would refuse at 2–13 rps while the CPU sat idle. What runs out is the event loop, which streams cost in proportion to their chunks per second, not their number.

**Final fix:**

| Decision | Why | Rejected alternative |
|---|---|---|
| **Shed on event-loop lag.** A background task measures how late the loop runs (smoothed, so one GC pause doesn't shed alone). While it is over `GATEWAY_MAX_LOOP_LAG_S` (50 ms), new requests get a 503 with `Retry-After` of 1–3 s, jittered so refused clients don't return together. | Lag is what actually runs out first, whatever the answers' length. | A fixed in-flight cap tuned to one answer length (the first fix). |
| **A hard cap per kind of work as a backstop:** 1,024 chats and 256 tool calls in flight per process. Tool calls are separate, so held approvals (up to 50 s each) can't take chat's places, and the other way round. | Bounds memory however the lag behaves. | One shared cap. |
| **Admission happens after the body is read and the key is checked** (in the route, and in the MCP tool handler), and the place is released when the response is over, however it ends. | A client without a key, or one that never finishes its body, holds nothing. A test holds five such sockets open against a cap of 1 and is still served. | Middleware at the door (the first fix). |
| **Each provider has a bulkhead** the size of its connection pool (`GATEWAY_UPSTREAM_MAX_CONNECTIONS`, 1,000). Past it, a call fails at once with `GatewayBusyError`, and the router moves on to the next target. | One slow provider can't take every place in the gateway, and nothing waits for a connection that won't come. | Waiting on the pool. |
| **`GatewayBusyError` never counts against a provider's circuit breaker**, including during a half-open trial, whose permit is freed. An empty connection pool (`PoolTimeout`, also mapped for Anthropic's SDK) raises it too. | It says this gateway is saturated, not that the provider is down. Counted, it opened every circuit at once. | — |
| **When every target is busy, the client gets 503 with `Retry-After`, not 502.** | It's our capacity, so the client should come back later. A 502 says the provider failed. | — |
| **The fake LLM has its own image** (`harness/Dockerfile`, with its own `.dockerignore`). **k6 runs from its image on the compose network.** | The gateway's image deliberately leaves `harness/` out. Nothing to install, and no port forwarding in the numbers. | — |

## Known gaps

- **One laptop, everything on it.** The numbers compare the two paths with each other. They aren't capacity figures for production hardware, and single runs vary by more than the effects under study: one 200 rps run right after a stack restart came out four times worse than its repeats.
- **One process.** More capacity comes from more replicas (Helm, step 10c). Rate limits and budgets are shared through Redis; admission is per process.
- **Lag shedding still lets the tail grow under overload** (p99 about 2.5 s at 130 % of capacity). A cap tuned to known answer lengths holds the tail better, but only for those lengths.
- **A slow provider that trickles bytes** never times out (the read timeout is per read) and can fill its own bulkhead. That provider is then skipped, but nothing detects slow calls as failures.
- **Slow-body clients still hold a connection and a socket** (not an admission place). Body-read deadlines belong at the proxy or ingress in front of the gateway.
- **No soak run.** Memory over hours (*Release It!*'s longevity) wasn't measured.

## Interview questions to be ready for

1. *Your gateway was fine at 200 rps and returned nothing but 502s at 400. Walk me through why.*
2. *Why shed on event-loop lag instead of capping requests in flight? When would a cap be the better choice?*
3. *Why does running out of connections to a provider not count against that provider's circuit breaker?*
