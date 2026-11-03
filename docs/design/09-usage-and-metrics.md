# 09 — Usage events, the usage worker, metrics and the dashboard

**Goal:** every chat request leaves one usage row in Postgres saying who asked, which provider answered, what it cost and how long it took. Writing that row must never slow down or fail the request. Operators get Prometheus metrics and one Grafana dashboard.

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
   take: own unacked entries, then other consumers' stale ones (XAUTOCLAIM), then new (XREADGROUP BLOCK)
   undecodable → gw:usage:dead + ack
   insert_many (ON CONFLICT (event_id) DO NOTHING) ─► ack + delete
   Postgres down → no ack, back off 1 s … 30 s; the entries are delivered again
   a constraint refuses the batch → insert one by one, dead-letter only the refused
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
| **One bad event can't block the queue:** undecodable entries and rows the database refuses go to `gw:usage:dead`. | A poison message retried forever stops every event behind it. | Retrying forever, or dropping silently. |
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
| `usage_worker_events_written_total`, `…_dead_lettered_total`, `…_failures_total` | — (worker, port 9101) |

The dashboard (`deploy/grafana/dashboards/gateway.json`, provisioned) shows:
- **From Prometheus:** traffic by outcome, time to first chunk and duration at p50 and p99, tokens per second, chat responses by status, breaker states, and the usage pipeline.
- **From Postgres:** tokens per tenant, requests by provider and outcome, and every MCP tool-call decision, read from the audit chains.

## Configuration

```bash
GATEWAY_USAGE_STREAM_MAX_LEN=100000
GATEWAY_USAGE_WORKER_METRICS_PORT=9101
```

## Known gaps (on purpose)

- **An interrupted stream is charged its whole reservation**, and the usage row says so (`charged_tokens` > prompt + completion). Counting the tokens actually streamed would need a tokenizer per model.
- **Recording adds one Redis round trip before `[DONE]`**, as the settle already did.
- **Dead letters are only parked**: nothing replays `gw:usage:dead` yet; a human reads it with `XRANGE`.
- **The init script runs only on a fresh Postgres volume**; an existing volume needs `docker compose down -v` (or create the `grafana_reader` role by hand).
- **Grafana runs with anonymous viewer access** for the local demo; Helm (step 10) sets real auth.

## Interview questions to be ready for

1. *Why is there a queue between the gateway and Postgres, and why Redis Streams and not Kafka?*
2. *The worker crashes after inserting a batch but before acknowledging it. What happens?*
3. *Why does no Prometheus metric carry a tenant label, and where do per-tenant numbers come from?*
