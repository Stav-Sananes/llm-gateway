# 09 — Usage events, the usage worker, metrics and the dashboard

**Goal:** every chat request that reaches a provider (or tries to) leaves one usage row in Postgres saying who asked, which provider answered, what it cost and how long it took. Writing that row must never slow down or fail the request. Operators get Prometheus metrics and one Grafana dashboard.

This is build step 6.

## How a usage event flows

```
ChatCompletions._metered (chat/use_case.py), when the stream ends however it ends:
   settle the budget ─► UsageRecorder.record(UsageEvent)          (shielded, ≤ 5 s)
                          │
   MeteredUsage (metrics.py): Prometheus counters/histograms, then
   RedisUsageStream (storage/usage_stream.py): XADD gw:usage MAXLEN ~100000
                          │   Redis down → event dropped, logged, gateway_usage_events_dropped_total
                          ▼
llm-gateway usage-worker (separate process) ─ UsageWorker (usage/worker.py)
   take: own unacked entries, then other consumers' stale ones (XAUTOCLAIM, which since Redis 7
         also drops entries MAXLEN trimmed), then new (XREADGROUP BLOCK)
   trimmed before stored (empty) → ack, counted lost;  undecodable → gw:usage:dead + ack
   insert_many (ON CONFLICT (event_id) DO NOTHING) ─► ack + delete
   insert fails and Postgres doesn't answer → no ack, back off 1 s … 30 s; delivered again
   insert fails but Postgres answers → the batch is the problem: insert one by one,
                                        dead-letter only the ones that fail
```

Which provider answered comes from the router: it marks the first chunk each provider serves with an `Origin(provider, model)`. That's also what step 8 (resume) needs for one usage row per provider.

## Decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| **A Redis Stream between the gateway and Postgres**, drained by a separate worker process. | The request path talks only to Redis, which it already depends on. A slow or down Postgres delays usage rows, never chat requests (Release It: decoupling middleware). Kill-safe: unacknowledged entries are delivered again. | Writing Postgres on the request path: database latency in every request. An in-process `asyncio.Queue`: lost on every restart or crash. |
| **At-least-once delivery, idempotent insert** on a UUID `event_id`. | Redelivery is how failures are recovered; the primary key makes a duplicate harmless. | Exactly-once: needs a transaction across Redis and Postgres. |
| **The stream is capped** (`MAXLEN ~ 100000`). Acknowledged entries are deleted. | No unbounded queues (SPEC §6). The cap only bites when the worker has been down long; then the oldest events go, not Redis's memory. | Uncapped: a dead worker eventually takes Redis down with it. |
| **Redis down → the event is dropped and counted**, the request is unaffected. | Usage rows are for billing and analytics; the budget in Redis is what enforces limits, and it already fails closed. A metric makes the loss visible. | Failing the request: metering would take the gateway down with it. |
| **Settle and record run shielded from cancellation**, with a 5 s cap. | A client hanging up cancels the request. Without the shield, the settle and the usage record were cancelled too: the tenant stayed charged its worst case and no usage row was written. An end-to-end test now hangs up mid-stream. | Fire-and-forget tasks: unbounded, and lost on shutdown. |
| **One bad event can't block the queue.** Undecodable entries, and rows that fail while Postgres answers `SELECT 1`, go to `gw:usage:dead`. | A poison message retried forever stops every event behind it. Review found that a NUL in the model name failed as a generic driver error, which looked like an outage, so the worker retried the batch forever. Classifying by the error alone isn't safe; asking whether the store is up is. | Retrying forever, or trusting the error class. |
| **Bad values are stopped at three layers.** The edge refuses model names with control characters or over 256 chars, `max_tokens` over 1,000,000, and bodies over 4 MiB. `decode` refuses what Postgres would. Token counts are `BIGINT`. | One client request could otherwise poison the pipeline for every tenant, or fill Redis with multi-MB events (the stream cap counts entries, not bytes). | One check at the edge only. |
| **Redis runs with an append-only file (`everysec`) on a volume.** | It holds the usage queue and budgets now, not just a cache: a crash loses at most a second of events. Verified: an entry survived `docker kill -s KILL`. | RDB snapshots only: a crash lost everything since the last snapshot. |
| **No foreign key from `usage_events` to `tenants`.** | An insert that fails a foreign key would be retried forever. Tenants are never deleted, and usage is analytics. | A foreign key: one stray tenant id blocks the pipeline. |
| **No tenant or client-chosen model label on any metric.** Per-tenant numbers come from Postgres, which Grafana queries directly. | Label values that clients control grow the metric set without bound. Paths are a fixed list (others are `other`), providers come from config. | `tenant` labels on Prometheus metrics. |
| **Grafana reads Postgres with its own read-only role**, granted only `tenants`, `usage_events` and `audit_events`. | Least privilege: the dashboard can't write, and can't read `api_keys`. | The gateway's credentials. |

## Metrics

| Metric | Labels |
|---|---|
| `gateway_http_requests_total`, `gateway_http_request_duration_seconds` | `path` (fixed list or `other`), `status` |
| `gateway_chat_requests_total` | `provider` (`none` if nothing answered), `outcome` (`completed`, `interrupted`, `failed`) |
| `gateway_chat_tokens_total` | `provider`, `kind` (`prompt`, `completion`) |
| `gateway_chat_time_to_first_chunk_seconds`, `gateway_chat_duration_seconds` | `provider` |
| `gateway_circuit_state` | `provider`: 0 closed, 1 half-open, 2 open (read at scrape time) |
| `gateway_usage_events_dropped_total` | — |
| `usage_worker_events_written_total`, `…_dead_lettered_total`, `…_lost_total`, `…_failures_total` | — (worker, port 9101) |
| `usage_stream_length`, `usage_stream_pending`, `usage_stream_lag` | — (worker, every 15 s): alert before the length nears the cap |

The dashboard (`deploy/grafana/dashboards/gateway.json`, provisioned) shows:
- **From Prometheus:** traffic by outcome, time to first chunk and duration at p50 and p99, tokens per second, chat responses by status, breaker states, and the usage pipeline.
- **From Postgres:** tokens per tenant, requests by provider and outcome, and every MCP tool-call decision, read from the audit chains.

## Configuration

```bash
GATEWAY_USAGE_STREAM_MAX_LEN=100000
GATEWAY_MAX_REQUEST_BYTES=4194304
GATEWAY_USAGE_WORKER_METRICS_PORT=9101
```

## Known gaps (on purpose)

- **An interrupted stream is charged its whole reservation**, and the usage row says so (`charged_tokens` > prompt + completion). Counting the tokens actually streamed would need a tokenizer per model.
- **Recording adds one Redis round trip before `[DONE]`**, as the settle already did.
- **Dead letters are only parked**: nothing replays `gw:usage:dead` yet; a human reads it with `XRANGE`.
- **The init script runs only on a fresh Postgres volume**; an existing volume needs `docker compose down -v` (or create the `grafana_reader` role by hand).
- **Grafana runs with anonymous viewer access** for the local demo. Any viewer can run SQL as `grafana_reader` against the three granted tables, including `audit_events` payloads. The role has a 5 s statement timeout. Helm (step 10) sets real auth.
- **`/metrics` is on the public listener**, unauthenticated. It shows provider names, traffic and breaker states, no tenant data. Helm keeps it off the ingress.
- **Requests refused up front** (rate limit, budget, bad key) **leave no usage row**; they show in `gateway_http_requests_total` by status.
- **Dead consumers pile up** in `XINFO CONSUMERS` (one per container ID); their pending entries are claimed after 60 s.

## Interview questions to be ready for

1. *Why is there a queue between the gateway and Postgres, and why Redis Streams and not Kafka?*
2. *The worker crashes after inserting a batch but before acknowledging it. What happens?*
3. *Why does no Prometheus metric carry a tenant label, and where do per-tenant numbers come from?*
