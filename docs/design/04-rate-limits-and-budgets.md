# 04 — Rate limits and token budgets

**Goal:** one tenant can't starve the others (rate limit), and can't spend more tokens per day than it's allowed (budget), even with many gateway replicas.

## How a request is checked

```
authenticated request ─► ChatCompletions.start (chat/use_case.py)
   1. rate limiter: take one slot from the tenant's bucket        no slot → 429 rate_limit_exceeded + Retry-After
   2. budget: reserve prompt estimate + max_tokens (worst case)   no room → 429 insufficient_quota
   3. stream from the provider
   4. when the stream ends, however it ends: settle the reservation to the provider's reported usage
```

Both checks run before the first chunk exists, so a refusal is a normal HTTP error, never a broken stream.

## Decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| **Token bucket per tenant in Redis, one Lua script, Redis's own clock.** | Shared across replicas. The script reads, refills and takes atomically, so two replicas can't both take the last slot, and `TIME` avoids clock skew between replicas. | A fixed window counter: allows 2× bursts at window edges. Python-side read-modify-write: races. |
| **Rate limit fails open to a per-replica bucket** when Redis is down. | Losing Redis shouldn't take the API down. Each replica still limits on its own: looser, never unlimited. | Failing closed: a cache outage becomes a full outage. Failing to "no limit": one tenant can flood the providers. |
| **Budget = tokens per tenant per UTC day; reserve the worst case, then settle.** Worst case = prompt estimate (chars ÷ 4 + 4 per message) + `max_tokens` (or `default_max_tokens`, 1024). | A streamed response's cost is only known at the end. Reserving up front means concurrent requests can't jointly overspend; settling refunds what wasn't used. | Charging after the fact: N parallel requests all pass a check that only one could afford. |
| **Reserve is one Lua script** (check and add together). | Two concurrent reservations can't both squeeze under the limit. A contract test fires 25 parallel reservations at a budget for 10. | GET then INCRBY: a race. |
| **Budget fails closed** (503) when Redis is down. | Spend is money. Without the counter there's no cap, so the safe answer is "not now". With budgets off (`GATEWAY_TOKEN_BUDGET_PER_DAY` unset), Redis being down only loosens rate limits. | Failing open: an outage becomes an unbounded bill. |
| **A broken stream keeps the whole reservation charged.** | No usage report means the real cost is unknown; the reservation is the upper bound. | Refunding everything: a provider that drops streams would make tokens free. |
| **Settling goes to the day the tokens were reserved in.** | A request that starts at 23:59:59 and ends after midnight must not charge tomorrow. | Settling into "today": costs leak across days. |
| **A settle that fails is logged, not raised.** | The client already has its answer; failing it now helps nobody. The reservation (the upper bound) stays charged. | Raising: a perfectly good response turns into an error event. |

## Configuration (environment, per tenant)

| Variable | Default |
|---|---|
| `GATEWAY_RATE_LIMIT_BURST` | 20 requests |
| `GATEWAY_RATE_LIMIT_PER_S` | 5 per second |
| `GATEWAY_TOKEN_BUDGET_PER_DAY` | 1,000,000 tokens (unset = no budget) |
| `GATEWAY_DEFAULT_MAX_TOKENS` | 1024 (reserved when a request sets none) |

## Known gaps (on purpose)

- **Same limits for every tenant.** Per-tenant plans need a `tenant_limits` table and an admin command; the ports don't change.
- **The prompt estimate is a heuristic.** It can be off for non-English text or images; settling corrects the counter afterwards, and the provider's own count is what's charged.
- **A response without `max_tokens` can cost more than its reservation**; settling then charges the extra, so a tenant can end a day slightly over budget.
- **Rate limits count requests, not tokens.** Token-per-minute limits would be a second bucket with a weight.

## Interview questions to be ready for

1. *Why reserve before streaming instead of charging the usage afterwards?*
2. *Redis is down. Why does the rate limit fail open but the budget fail closed?*
3. *Why does the token bucket use Redis's clock and not the gateway's?*
