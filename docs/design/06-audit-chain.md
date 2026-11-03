# 06 — Tamper-evident audit chain

**Goal:** every audited action (MCP tool calls, from step 5c) is written to a per-tenant log in which editing, deleting or reordering any record is detectable, and secrets never enter that log.

Step 5 is split into three PRs: this one (the chain), the Cedar policy engine, and the MCP aggregator that uses both.

## How it works

```
AuditLog.record(tenant, action, details)            (audit/log.py)
   1. redact(details)                               keys ending in password/token/secret/api-key/…
                                                    (whole words: max_tokens stays), and values that
                                                    look like keys (gw_…, sk-…, Bearer …, JWTs, PEM)
   2. canonical JSON: sorted keys, no spaces, ASCII-only, UTC time
   3. PostgresAuditStore.append                     one transaction:
        lock the tenant's audit_heads row (FOR UPDATE)
        hash_n = SHA-256(hash_(n-1) bytes ‖ canonical_n)      hash_0's predecessor = 64 zeros
        INSERT audit_events (seq n, prev_hash, hash, payload)
        UPDATE audit_heads (seq n, hash_n)

llm-gateway audit verify TENANT                    read the head first, then recompute every link
                                                    up to it in pages of 1,000
   ok: N records, head <hash>        exit 0
   TAMPERED: seq K: <what is wrong>  exit 2
```

## Decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| **One hash chain per tenant**, SHA-256 over the previous hash and the canonical event. | Changing any record changes its hash, which no longer matches the next record's `prev_hash`. Per tenant, so tenants don't contend on one lock and a tenant's trail can be exported alone. | One global chain: every tool call in the system serializes on one row. |
| **Canonical JSON is stored as TEXT, byte for byte.** | Verification must hash exactly what was hashed at write time. JSONB reorders keys and normalizes numbers, so its text would not hash the same. | JSONB: queryable, but unverifiable. |
| **A head row per tenant, locked `FOR UPDATE` on every append.** | Concurrent appends for one tenant wait in line, so each links onto the true latest hash; a contract test fires 25 parallel appends. The head also lets `verify` notice a cut-off tail, which a chain alone can't. | Reading `MAX(seq)` without a lock: two appends link onto the same parent and fork the chain. |
| **Redaction before hashing.** | A secret that is hashed into the chain can never be removed without breaking it. Redacting first means there is nothing to remove. | Redacting on read: the secret is still in the database and backups. |
| **Appends are synchronous and failures stop the action** (`AuditUnavailableError`). | The spec requires every tool call to be audited; an unaudited call is worse than a refused one. | Async/queued audit: a crash loses records silently. |
| **`verify` reads the head first and checks the chain up to it** (keyset pages on seq), then requires the last hash to equal the head's. Each payload's tenant must be the tenant being verified. | A tenant that keeps writing during `verify` must not look tampered (a reviewer reproduced 16 false alarms in 20 runs when the head was read last). Pages, because a chain can have millions of records (Release It: Unbounded Result Sets). | Head last: false alarms on busy tenants. Loading the whole chain. |
| **`seq > 0` is a database constraint.** | `verify` starts at seq 1; rows below it would never be checked. | Trusting writers. |
| **Canonical JSON is ASCII-only; non-JSON values become `str()`; non-string keys are refused.** | Any string (even a lone surrogate from a JSON-parsed tool argument) can be encoded and hashed; `{1: …, "1": …}` can't collapse two events into one text. Anything still unrenderable raises `UnauditableEventError` before the store is touched. | `ensure_ascii=False`: one odd character turns an audit into a crash. |

## Threat model, honestly

Tamper-*evident*, not tamper-*proof*. Someone who can write to `audit_events` **and** `audit_heads` can rewrite a tenant's entire chain consistently. Two defences, neither in this PR:

- **Anchoring:** `verify` prints the head hash. Publishing it somewhere the database can't reach (a ticket, a signed log, object storage with retention) pins the chain up to that point.
- **Least privilege:** the gateway's database role needs `SELECT`/`INSERT` on `audit_events` and `SELECT`/`INSERT`/`UPDATE` on `audit_heads`; denying `UPDATE`/`DELETE` on `audit_events` and `DELETE` on `audit_heads` makes casual edits impossible.

What a chain alone can't show, by design: deleting **all** of a tenant's events together with its head row looks like a tenant that never had any (anchor the head to catch it); `recorded_at` is not hashed (`occurred_at` is); and `occurred_at` is taken before the tenant's lock, so two near-simultaneous events can carry timestamps in the opposite order to their seq.

## Known gaps (on purpose)

- Nothing records events yet; the MCP aggregator (step 5c) is the first producer.
- No retention or export command.
- Redaction is pattern-based; a secret in an unexpected shape under an innocent key gets through. `AuditLog(store, secret_keys=[...])` adds key names. Code that calls `AuditStore.append` directly skips redaction; only `AuditLog` is meant to be used.

## Interview questions to be ready for

1. *An attacker deletes one row from the middle of the log. How does `verify` notice? And the last row?*
2. *Why store the payload as TEXT instead of JSONB?*
3. *Why redact before hashing rather than when reading?*
