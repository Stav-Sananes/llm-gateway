# 10 — The semantic cache

**Goal:** a question asked before, in other words, is answered from the cache: free for the tenant and with no model call. A false hit (the wrong answer to a different question) is worse than a miss, so the rules are strict. The cache must never fail or noticeably slow a request.

This is build step 7.

## How a request meets the cache

```
ChatCompletions.start: rate limit ─► SemanticCache.look_up ─► hit: replay   (nothing reserved)
                                                  └─► miss/bypass: budget reserve ─► provider
  cache_key(request)            (cache/model.py)  None → bypass, no embedding at all:
     tools offered, temperature unset or > 0.3, a tool message, last message not a user's,
     anything but text in any message (an image), or Cache-Control: no-store
  store breaker open?           → bypass, and don't pay for an embedding either
  embed the last question       (embedder breaker; embed + search share one 1 s deadline)
  Qdrant: 3 nearest points WHERE tenant_id = … AND context_hash = … AND expires_at > now,
          cosine ≥ 0.95                            (store breaker; skipped with no-cache)
  word-for-word guard           first candidate whose question also agrees lexically
     hit  → replay: one content chunk, finish_reason, usage 0/0; Origin("cache", model)
     miss → the provider streams; an answer that finishes with "stop" (≤ 64 KiB) is queued
            and a background writer stores it with expires_at = now + TTL
```

The response carries `X-Gateway-Cache: hit` or `miss`. At startup the gateway embeds a fixed text once, retrying for up to a minute, so a cold embedding model is loaded before the first question needs it.

## Decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| **Only the last user message is matched by meaning; everything before it is matched exactly** (a SHA-256 of model, `max_tokens`, `temperature`, whether tools were offered, the system prompt, earlier turns, and the last message's other fields). | "What about the second one?" means different things in different conversations. Embedding the whole conversation would let a long shared prefix dominate the score. | Embedding all messages together. |
| **The lookup runs before the budget reservation.** | A hit costs the tenant nothing, so it must not need budget, and a lookup that fails or is cancelled (the client left) can't leave a reservation behind. The rate limit still comes first: a hit is a request too. | Reserving first, then refunding on a hit. |
| **A word-for-word guard on top of similarity.** A candidate is a hit only if its numbers and negation words (not, never, no, …) match exactly, no word flips to its opposite (safe/unsafe, able/unable, agree/disagree), and ≥ 80 % of the words are shared. The 3 nearest candidates are checked. | Real embeddings score "Is it safe to …" and "Is it unsafe to …" above 0.95, and "2 servers" next to "3 servers". Against the real `nomic-embed-text` model those pairs scored as hits before the guard and miss after it. The cost: only close rewordings hit (case, punctuation, a word or two), not free paraphrases. | Trusting the score alone; a lower threshold. |
| **A question over 1,000 characters matches only word for word** (its full text goes into the context hash). | An embedding model reads only its first few hundred tokens: "<long document> Summarize it" and "<same document> Translate it" embed the same. | Embedding a truncated question. |
| **One circuit breaker per dependency** (`cache-embedder`, `cache-store`); any exception, including a malformed reply, counts as a failure. | A healthy embedder must not hide a hung Qdrant, and an open store circuit also skips the embedding, which could only be wasted. | One breaker for "the cache". |
| **Answers are stored by a background writer, through a bounded queue (100) that drops when full** (`gateway_cache_writes_dropped_total`). | Storing must not slow the response or hold its shielded finish; a slow Qdrant must not grow memory without bound. A dropped write costs one future miss. | Writing inside the request's finish. |
| **Qdrant takes an API key and runs with a memory cap (1 GiB)**; the port is bound to localhost. | Every tenant's cached answers live there; a cache entry is trusted as much as a Postgres row. A full Qdrant crashes, and the gateway carries on uncached, instead of the host running out of memory. | An open Qdrant on the Docker network. |
| **Cacheable only with `temperature` set and ≤ 0.3, no tools, no tool messages, text only.** A missing temperature counts as "sampling", which is the provider's default. | At higher temperature the client asked for variety. An answer to a request with tools may be a tool call, which depends on state the cache can't see. An image changes the question in ways its text doesn't show. | Caching everything above a similarity score. |
| **Cosine ≥ 0.95 by default, configurable; the threshold and the hit scores are on the dashboard.** | False hits hide just above the threshold; the p10 hit score next to the threshold shows how close they come. | A lower default for a better hit rate. |
| **One Qdrant collection, every search filtered by `tenant_id` and `context_hash` (both indexed).** | Tenants never see each other's answers, which a test checks against real Qdrant. One collection keeps operations simple. | A collection per tenant: thousands of collections, each with its own index overhead. |
| **TTL through an `expires_at` payload.** Searches skip expired points, and every 100th write sweeps them. | Qdrant has no TTL. Answers go stale (prices, dates, a fixed bug). | No expiry: the cache grows forever and serves stale answers. |
| **Only answers that finished cleanly (`stop`), up to 64 KiB, are stored.** | A truncated (`length`), interrupted or failed answer must not be replayed as if it were whole. | Storing whatever streamed. |
| **A hit is free:** usage 0/0, the budget settles at 0, and the usage row says `provider = cache`. | The tenant didn't use a model. The row still shows the request happened. | Charging the original tokens. |
| **The cache never fails a request.** Every error or timeout is a bypass, and the circuit breakers (same thresholds as the providers') stop trying for 30 s after 5 failures. | SPEC §6: "Qdrant down: the cache is bypassed and requests continue". The breaker keeps a dead Qdrant from adding its timeout to every request. Tested with Qdrant down: 8 requests answered and 5 embeddings tried. | Failing closed, or paying the timeout on every request. |
| **`Cache-Control: no-cache`** forces a fresh answer, which may still be stored; **`no-store`** bypasses the cache both ways. | The standard HTTP header that clients and proxies already understand. | A custom opt-out header. |
| **Collections are created on first use, not at startup**, and the Qdrant client's version check is off. | The gateway must start, and serve uncached, without Qdrant. | Creating the collection at startup: Qdrant becomes a hard dependency. |
| **Embeddings come over the OpenAI `/embeddings` protocol** (Ollama `nomic-embed-text`, 768 dims, by default from the chat upstream). A vector of the wrong size, or a collection built for another size, is reported, not used. | One more integration with no new client. Switching embedding models without dropping the collection would fail every search; the error says what to do. | A Python embedding library inside the gateway: hundreds of MB and CPU on the request path. |

## Configuration

```bash
GATEWAY_CACHE_ENABLED=true
GATEWAY_QDRANT_URL=http://qdrant:6333
GATEWAY_QDRANT_API_KEY=                   # compose: QDRANT_API_KEY, default local-dev-qdrant-key
GATEWAY_CACHE_EMBEDDING_BASE_URL=          # unset: the chat upstream (Ollama)
GATEWAY_CACHE_EMBEDDING_MODEL=nomic-embed-text
GATEWAY_CACHE_EMBEDDING_DIMS=768
GATEWAY_CACHE_SIMILARITY_THRESHOLD=0.95
GATEWAY_CACHE_TTL_S=3600
GATEWAY_CACHE_TIMEOUT_S=1.0
```

## Checked against the real stack

Compose stack, Ollama `qwen2.5:0.5b` for chat and `nomic-embed-text` for embeddings, Qdrant with its API key, temperature 0:

| Questions, in order | Result |
|---|---|
| "Is it safe to delete the users table?" then "Is it **un**safe …" | miss: the guard rejects the flip |
| "is it safe to delete the users table?" after "Is it safe …" (case only) | hit, score 0.99999 |
| A 2,000-character document + "Summarize it", then the same document + "Translate it to French" | miss: long questions match word for word |
| Any answer cut off by `max_tokens` (`finish_reason: length`) | never stored |

The first run found a real problem: on a cold start Ollama needs 1–2 s to load the embedding model, longer than the 1 s lookup deadline, so the first lookups timed out and opened the embedder's circuit. Two fixes: the gateway warms the embedder at startup (outside the breaker), and compose sets `OLLAMA_KEEP_ALIVE=24h` so the model isn't unloaded after five idle minutes.

## Known gaps (on purpose)

- **`X-Gateway-Cache` says `miss` for ineligible and bypassed requests too.** The metric tells them apart (`result="ineligible"` / `"unavailable"`); a `bypass` header value would make debugging one request easier.
- **Only close rewordings hit.** The word-for-word guard trades hit rate for never giving the answer to a different question. A cross-encoder or an LLM check on the candidate would let real paraphrases through, at the cost of a model call per lookup.
- **A miss pays for the embedding** (one call to the embedding model: 0.2–0.5 s for `nomic-embed-text` on a CPU-only laptop, far less on a GPU) before the provider is asked. Lookup and provider could start in parallel, but a hit would then cost an unused model call.
- **Writes don't wait for indexing** (`wait=False`): an identical question asked within milliseconds can still miss.
- **No negative caching or deduplication of in-flight identical questions**: ten identical questions at once all go to the model.
- **The cache is keyed by the requested model**, so a routed name (`smart`) and the model it routes to are different keys.
- **The fake embedder in tests is a bag of words**: it shows the plumbing, not how well a real model judges paraphrases. The real-stack checks above are manual; thresholds should be tuned on real traffic, with the dashboard's hit-score panel.

## Interview questions to be ready for

1. *How do you stop the cache from answering a different question that only looks similar?*
2. *Why is only the last message embedded, and the rest hashed?*
3. *Qdrant is down. What does a request cost now, and how do you know?*
4. *Why does the lookup happen before the budget reservation, and what would go wrong the other way round?*
