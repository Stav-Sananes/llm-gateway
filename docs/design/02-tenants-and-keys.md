# 02 — Tenants and API keys

**Goal:** an operator can create tenants and issue API keys for them, and the gateway can later turn a key into a tenant with one lookup. This PR covers storage and administration; checking the key on every request (bearer auth, Redis cache) is the next PR.

## How the pieces fit

```
operator ─► cli.py ─► tenancy/admin.py (TenantAdmin: the rules) ─► TenantStore (port)
                       uses tenancy/keys.py                          ├─ InMemoryTenantStore  (tests)
                                                                     └─ PostgresTenantStore  (storage/)
```

`tenancy/` imports nothing from SQLAlchemy or argparse. The CLI is a thin shell: parse the arguments, call `TenantAdmin`, print the result.

## Decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| **Key format `gw_<8 hex prefix>_<secret>`**, with a 256-bit secret from `secrets`. | The prefix names a key in logs and in `revoke-key` without revealing it. The `gw_` scheme makes leaked keys easy to spot with secret scanners. | An opaque random string. Nothing safe to show or search for. |
| **Store only the SHA-256 of the key**, never the plaintext. The plaintext is printed once. | A database leak exposes no usable keys. | Storing the key. One leak and every tenant is compromised. |
| **SHA-256, not bcrypt or argon2.** | Slow hashes protect weak, human-chosen passwords. A 256-bit random secret can't be guessed at any hash speed, and auth runs on every request, so 100 ms per request would be pure cost. | bcrypt. No security gain, large latency cost. |
| **Look keys up by hash** (`tenant_for_key(key_hash)`), with a unique index on the hash. | The store never sees a plaintext key, and the lookup is a single indexed read. | Lookup by prefix. The prefix isn't secret, so knowing it would be enough to get in. |
| **Revoking marks the key (`revoked_at`) instead of deleting it.** | Audit entries (step 5) must still trace a key to its tenant months later. Revoking twice keeps the first time. | Deleting the row. The audit trail would point at nothing. |
| **`TenantStore` port with an in-memory fake**, and contract tests that run every case against the fake *and* Postgres. | Rules are tested in milliseconds without a database, and the fake can't quietly drift from real behaviour. | Mocking SQLAlchemy. That tests the mocks, not the SQL. |
| **SQLAlchemy Core plus hand-written Alembic migrations**, shipped inside the package. | Explicit SQL with no ORM session magic. Migrations are frozen snapshots, and the image can run `llm-gateway migrate` itself. A test fails if `tables.py` and the migrations ever disagree. | An ORM with `create_all()`. No history, and production schemas drift. |
| **Named constraints** (`uq_tenants_name`). | The store can tell which rule a write broke and raise `DuplicateTenantError`, rather than guessing from the error text. | Catching every `IntegrityError` as "duplicate". It would hide real bugs. |
| **Bounded waits on the database**: pool checkout 2 s, connect 2 s, each statement 5 s. | A slow or hung Postgres fails fast instead of piling up requests (Release It: Timeouts). | Library defaults, which wait much longer or forever. |
| **Compose Postgres on host port 5433.** | A locally installed Postgres often holds 5432, and the connection silently goes to the wrong server. | 5432. It broke on the first try. |

## Using it

```bash
docker compose up -d postgres migrate
docker compose run --rm --no-deps gateway llm-gateway admin create-tenant acme
docker compose run --rm --no-deps gateway llm-gateway admin create-key acme   # prints the key once
docker compose run --rm --no-deps gateway llm-gateway admin revoke-key <prefix>
```

## Known gaps (on purpose)

- Nothing checks keys on requests yet. Bearer auth and the Redis key cache come in the next PR ([03](03-bearer-auth.md)).
- Prefix collisions: 8 hex chars give about 4 billion prefixes, so a collision becomes likely only after tens of thousands of keys. When one happens, `create-key` fails on the primary key and the operator retries. A retry loop can come later.
- There's no `list-keys` or `delete-tenant` command.

## Interview questions to be ready for

1. *Why is plain SHA-256 fine for API keys when it's wrong for passwords?*
2. *A key is revoked. Why keep its row instead of deleting it?*
3. *How do you know the in-memory fake behaves like Postgres?*
