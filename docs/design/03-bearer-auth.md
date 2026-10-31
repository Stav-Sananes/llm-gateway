# 03 — Bearer auth and the key cache

**Goal:** every API request carries `Authorization: Bearer gw_...`, and the gateway turns that key into a tenant fast, without the database on the hot path, and stays correct when Redis or Postgres fails.

## How a request is checked

```
request ─► http/auth.py (read the header) ─► Authenticator (tenancy/auth.py)
                                               1. shape check: gw_<8 hex>_<43 chars>?   no → 401
                                               2. SHA-256 the key
                                               3. KeyCache (Redis)      hit → tenant
                                               4. TenantStore (Postgres) miss → look up, then cache it
                                               unknown or revoked → 401
```

`tenancy/` still imports no FastAPI, Redis or SQLAlchemy. The cache is a port (`KeyCache`) with an in-memory fake, and contract tests run every case against the fake *and* real Redis, the same way 02 does for the store.

## Decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| **Cache-aside in Redis, keyed by the key's hash**, value = tenant id and name. | Auth runs on every request. A Redis read is far cheaper than a Postgres join, and the cache holds no plaintext keys either. | Caching in process memory. Each replica would need its own revocation signal. |
| **Entries expire after 60 s** (`key_cache_ttl_s`). | A safety net: if an eviction is ever missed, a revoked key stops working within a minute anyway. | No TTL. One missed eviction keeps a revoked key alive forever. |
| **Revoking evicts the cache entry.** Postgres is updated first, then Redis. | A revoked key must stop working now, not in 60 s. Committing first means a Redis failure can't undo the revocation. | Waiting for the TTL. Too slow for a leaked key. |
| **Revoke with Redis down still revokes, then exits 1** with "the key cache could not be cleared". | The operator must know the key may work for up to the TTL, and can retry. | Failing silently. The operator would believe the key is dead. |
| **Redis down → read Postgres** (timeout 0.25 s). | Postgres is the source of truth, so auth stays *correct*, only slower (Release It: fail over to the authority, never to "allow"). | Failing open. Anyone would get in. |
| **Postgres down → cached keys keep working; uncached keys get a 503.** | A 503 says "try again"; a 401 would tell a paying client its valid key is bad. | Answering 401 or 500. Both lie about what went wrong. |
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

- **No negative cache.** A flood of well-shaped but unknown keys goes to Postgres every time. A per-IP limit belongs with rate limiting (step 3).
- **No circuit breaker on Redis.** A *hanging* Redis costs each request up to 0.25 s before falling back; a refused connection fails at once. A breaker comes with the provider breakers (step 4).
- **Handlers don't see the tenant yet.** The check runs as a router dependency; rate limits and budgets (step 3) will need the tenant inside the handler.
- **Stale tenant data for up to the TTL** if a tenant were renamed. There is no rename command, so nothing triggers it today.

## Interview questions to be ready for

1. *Redis is down. Why does the gateway still answer correctly, and what does it cost?*
2. *Why update Postgres before deleting the Redis entry, and not the other way round?*
3. *Postgres is down and a key isn't cached. Why 503 and not 401?*
