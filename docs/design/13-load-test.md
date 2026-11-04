# 13 — Load test: what the gateway adds, and where it bends

**Goal (SPEC §7):** k6 against our fake LLM, which has a fixed latency. Report the latency the gateway adds, at p50 and p99, for a given RPS.

This is build step 10a. The run found a real defect: an overloaded gateway opened its provider's circuit. The fix is part of this step.

## How it's measured

```
load/run.sh [RPS] [DURATION]
  docker compose -f compose.yaml -f load/compose.load.yaml up        (the full stack)
     fake-llm: 20 tokens, 10 ms apart, about 200 ms per answer     (harness/Dockerfile)
     gateway → fake-llm, tenant limits lifted, cache off; tracing on, as in compose
  k6 (grafana/k6, on the compose network), constant arrival rate, streamed requests:
     round 1: fake LLM directly     round 1: through the gateway
     round 2: fake LLM directly     round 2: through the gateway      (alternating: same drift)
  each request checks status 200 and a final [DONE]
```

Both paths are measured the same way: the whole streamed answer (`http_req_duration`) and the time to the first byte (`http_req_waiting`). The fake LLM sends its headers at once and its first token 10 ms later. The gateway sends its headers only with the first token, after auth, the rate limit, the budget and the provider's first chunk. So "first byte through the gateway" compares with about 11 ms directly.

Machine: Apple M4 laptop, Docker Desktop with 10 CPUs and 8 GiB. One gateway process (uvicorn, one worker), the whole stack on the same machine as k6.

## Results

**50 requests/s, two 30 s rounds each.** Every request returned 200 and finished with `[DONE]`.

| | whole answer p50 | p99 | first byte p50 | p99 |
|---|---|---|---|---|
| fake LLM directly (round 1 / 2) | 239 / 232 ms | 266 / 254 ms | 1.0 / 0.5 ms | 2.7 / 2.5 ms |
| through the gateway (round 1 / 2) | 233 / 231 ms | 261 / 267 ms | 14.1 / 13.6 ms | 24.5 / 20.9 ms |

**What the gateway adds at 50 rps:** about **3 ms at p50 and 10–14 ms at p99 before the first token** (14 ms and 21–25 ms, against the fake's 11 ms first token). Nothing measurable over the whole answer: the difference is smaller than the run-to-run noise. Each token is passed on as it arrives, so the gateway's cost sits at the start.

**Where it bends (one process, 20 s at each rate):**

| rate | without shedding (before the fix) | shedding at 64 in flight (default) | shedding at 96 |
|---|---|---|---|
| 100 rps | all served, p99 311 ms | — | — |
| 200 rps | all served, p99 366 ms | 2.1 % shed, p99 355 ms | 0.3 % shed, p99 428 ms |
| 300 rps | — | 13.8 % shed, p99 335 ms | 8.4 % shed, p99 509 ms |
| 400 rps | **88 % failed (502), the provider's circuit opened**, p99 10.5 s | 36 % shed, p99 368 ms, circuit closed | 30 % shed, p99 1.26 s |

One process tops out at about **250–280 answers per second** of this size. Past that, the cap decides whether you get errors with a bounded tail, or slower answers for everyone.

## The defect the load found

At 400 rps, the single event loop fell behind:
- Redis calls hit their 250 ms timeouts, so rate limits failed open and budget settles were lost.
- Waits for one of the 100 upstream connections timed out.

The adapter reported those pool timeouts as "ollama timed out", the provider's circuit breaker counted them, and the circuit opened. From then on every request got a 502, although the fake LLM was idle (directly, it served 400 rps at p99 242 ms). A local overload had turned into a full outage: the chain reaction and cascading failure that *Release It!* warns about.

## Decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| **Shed load at the door:** at most `GATEWAY_MAX_CONCURRENT_CHATS` (64) chat requests in flight per process; past it, an immediate 503 with `Retry-After: 1`, counted in `gateway_chats_shed_total`. Each request counts from its first byte to its last, streaming included. | A fast refusal for some requests keeps the rest fast, and keeps the event loop from falling behind on everyone's Redis and provider calls. The check runs before the body is read or the key checked, so a shed request costs nothing. | No cap: at 400 rps everything failed. A queue in front: it only adds waiting, and SPEC §6 says no unbounded queues. |
| **64 by default, below the 100 upstream connections.** | Measured: 64 keeps p99 under 400 ms at twice the capacity, while 96 lets p99 grow past a second. Staying below the pool size means the pool never runs out first. | 96: about 10 % more answers served, at three times the tail latency. |
| **An empty connection pool is `GatewayBusyError`, which doesn't count against the provider's circuit breaker.** It is still a `ProviderUnavailableError`, so the router can try the next target. | It says this gateway is saturated, not that the provider is down. Counted, it opened every circuit at once. | Raising the pool size: it moves the point where it happens, not the cascade. |
| **The fake LLM gets its own image** (`harness/Dockerfile`, with its own `.dockerignore`). | The gateway's image deliberately leaves `harness/` out. | Shipping the harness inside the production image. |
| **k6 runs from its Docker image, on the compose network.** | Nothing to install; the numbers don't include Docker Desktop's port forwarding. | k6 on the host, through published ports. |

## Known gaps

- **One laptop, everything on it**: the gateway, the fake LLM, Postgres, Redis, Jaeger and k6 share 10 CPUs. The numbers compare the two paths with each other; they aren't capacity figures for production hardware.
- **One process.** More capacity comes from more replicas (Helm, step 10c), not from tuning one event loop. Rate limits and budgets are shared through Redis, so replicas agree. The load cap is per process.
- **Tracing stays on**, as in compose. Its cost is included in the numbers, not measured on its own.
- **Time to the first token through the gateway is read from the first byte**, because the gateway sends nothing before the first token. Directly, it is inferred from the fake's fixed 10 ms.
- **No soak run.** Memory over hours (*Release It!*'s longevity) wasn't measured.

## Interview questions to be ready for

1. *Your gateway was fine at 200 rps and returned nothing but 502s at 400. Walk me through why.*
2. *Why does running out of connections to a provider not count against that provider's circuit breaker?*
3. *Shedding at 64 or 96 in flight: what are you trading, and how did you choose?*
