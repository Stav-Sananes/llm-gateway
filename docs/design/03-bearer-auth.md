# 03 — Bearer auth and the key cache

**Goal:** every API request carries `Authorization: Bearer gw_...`, and the gateway turns that key into a tenant fast, without the database on the hot path, and stays correct when Redis or Postgres fails.

## How a request is checked

```
request ─► http/auth.py (read the header) ─► Authenticator (tenancy/auth.py)
                                               1. shape check: gw_<8 hex>_<43 chars>?   no → 401
                                               2. SHA-256 the key
                                               3. KeyCache (Redis)       tenant → serve; revoked mark → 401
                                               4. TenantStore (Postgres) miss → look up, cache it if absent
                                               unknown or revoked → 401
```

`tenancy/` still imports no FastAPI, Redis or SQLAlchemy. The cache is a port (`KeyCache`) with an in-memory fake, and contract tests run every case against the fake *and* real Redis, the same way 02 does for the store.

## Decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| **Cache-aside in Redis, keyed by the key's hash**, value = tenant id and name. | Auth runs on every request. A Redis read is far cheaper than a Postgres join, and the cache holds no plaintext keys either. | Caching in process memory. Each replica would need its own revocation signal. |
| **Entries expire after 60 s** (`key_cache_ttl_s`). | A safety net: if a revoke never reaches Redis, the key stops working within a minute anyway. | No TTL. One lost revoke keeps the key alive forever. |
| **Revoking writes a "revoked" mark into the cache** (not a delete), and caching a lookup only writes if the entry is absent (`SET NX`). Postgres is updated first, then Redis. | Closes a race a reviewer reproduced: a request reads Postgres just before the revoke, the revoke deletes the entry, then the request writes the key back for 60 s. With the mark, that late write is a no-op. Committing first means a Redis failure can't undo the revocation. | Deleting the entry. Leaves the race open. Waiting for the TTL. Too slow for a leaked key. |
| **Revoke with Redis down still revokes, then exits 1** with "the key cache could not be cleared". | The operator must know the key may work for up to the TTL, and can retry. | Failing silently. The operator would believe the key is dead. |
| **Redis down → read Postgres** (timeout 0.25 s, one Redis call per request: no cache write after a failed read). | Postgres is the source of truth, so auth stays *correct*, only slower (Release It: fail over to the authority, never to "allow"). | Failing open. Anyone would get in. |
| **An unreadable cache entry is a miss** and is deleted. | A bad value must cost one Postgres lookup, not a 500. | Trusting the parse. Any stray value breaks auth for that key. |
| **Postgres down → cached keys keep working; uncached keys get a 503**, logged as an error (cause type only: the driver message carries the key hash). | A 503 says "try again"; a 401 would tell a paying client its valid key is bad. | Answering 401 or 500. Both lie about what went wrong. |
| **Redis and Postgres are published on 127.0.0.1 only** in Compose. | Anyone who can write either one can mint access: a forged cache entry authenticates (a reviewer proved it). Both are inside the trust boundary. | Publishing on all interfaces with demo passwords. |
| **Shape check before any lookup.** | Garbage tokens (`sk-...`, empty, truncated) are refused without touching Redis or Postgres. | Hashing and looking up everything. Free load for scanners. |
| **One 401 for every failure**, OpenAI shape, code `invalid_api_key`, plus `WWW-Authenticate: Bearer`. | OpenAI SDKs already handle this error. One message gives an attacker nothing to tell "unknown" from "revoked". | Distinct messages per cause. They leak which keys exist. |

## Using it

```bash
docker compose up -d --build
KEY=$(docker compose run --rm --no-deps gateway llm-gateway admin create-key acme)
curl localhost:8080/v1/chat/completions -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5:0.5b","messages":[{"role":"user","content":"Hi"}]}'
```

## Known gaps (on purpose)

- **The cache is trusted, not verified.** Signing cache values (HMAC with a server secret) would stop a Redis-only attacker; today Redis is treated like Postgres. Real deployments need Redis AUTH/TLS.
- **The request body is parsed before the key is checked** (FastAPI runs body validation before dependencies), so an unauthenticated client can get a 400 for bad JSON. It learns nothing about keys.
- **No negative cache.** A flood of well-shaped but unknown keys goes to Postgres every time. A per-IP limit belongs with rate limiting (step 3).
- **No circuit breaker on Redis.** A *hanging* Redis costs each request up to 0.25 s before falling back; a refused connection fails at once. A breaker comes with the provider breakers (step 4).
- **Handlers don't see the tenant yet.** The check runs as a router dependency; rate limits and budgets (step 3) will need the tenant inside the handler.
- **Stale tenant data for up to the TTL** if a tenant were renamed. There is no rename command, so nothing triggers it today.

## Interview questions to be ready for

1. *Redis is down. Why does the gateway still answer correctly, and what does it cost?*
2. *Why does revoke write a "revoked" mark into Redis instead of deleting the entry? Walk through the race.*
3. *Postgres is down and a key isn't cached. Why 503 and not 401?*
