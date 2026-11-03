# 10 — The semantic cache

**Goal:** a question asked before, in other words, is answered from the cache: free for the tenant and with no model call. A false hit (the wrong answer to a different question) is worse than a miss, so the rules are strict. The cache must never fail or noticeably slow a request.

This is build step 7.

## How a request meets the cache

```
ChatCompletions.start: rate limit ─► budget reserve ─► SemanticCache.look_up ─► provider or replay
  cache_key(request)            (cache/model.py)  None → bypass, no embedding at all:
     tools offered, temperature unset or > 0.3, a tool message, last message not a user's,
     anything but text in it (an image), or Cache-Control: no-store
  breaker open?                 → bypass          (Qdrant or the embedder failed 5 times)
  embed the last question       (≤ 1 s with the search, else bypass)
  Qdrant: nearest point WHERE tenant_id = … AND context_hash = … AND expires_at > now,
          cosine ≥ 0.95                            (skipped with Cache-Control: no-cache)
     hit  → replay: one content chunk, finish_reason, usage 0/0; Origin("cache", model)
     miss → the provider streams; when the answer finishes with "stop" (and ≤ 64 KiB),
            it's stored with expires_at = now + TTL (inside the shielded finish, see 09)
```

The response carries `X-Gateway-Cache: hit` or `miss`.

## Decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| **Only the last user message is matched by meaning; everything before it is matched exactly** (a SHA-256 of model, `max_tokens`, system prompt and earlier turns). | "What about the second one?" means different things in different conversations. Embedding the whole conversation would let a long shared prefix dominate the score. | Embedding all messages together. |
| **Cacheable only with `temperature` set and ≤ 0.3, no tools, no tool messages, text only.** A missing temperature counts as "sampling", which is the provider's default. | At higher temperature the client asked for variety. An answer to a request with tools may be a tool call, which depends on state the cache can't see. An image changes the question in ways its text doesn't show. | Caching everything above a similarity score. |
| **Cosine ≥ 0.95 by default, configurable; the threshold and the hit scores are on the dashboard.** | False hits hide just above the threshold; the p10 hit score next to the threshold shows how close they come. | A lower default for a better hit rate. |
| **One Qdrant collection, every search filtered by `tenant_id` and `context_hash` (both indexed).** | Tenants never see each other's answers, which a test checks against real Qdrant. One collection keeps operations simple. | A collection per tenant: thousands of collections, each with its own index overhead. |
| **TTL through an `expires_at` payload.** Searches skip expired points, and every 100th write sweeps them. | Qdrant has no TTL. Answers go stale (prices, dates, a fixed bug). | No expiry: the cache grows forever and serves stale answers. |
| **Only answers that finished cleanly (`stop`), up to 64 KiB, are stored.** | A truncated (`length`), interrupted or failed answer must not be replayed as if it were whole. | Storing whatever streamed. |
| **A hit is free:** usage 0/0, the budget settles at 0, and the usage row says `provider = cache`. | The tenant didn't use a model. The row still shows the request happened. | Charging the original tokens. |
| **The cache never fails a request.** Every error or timeout is a bypass, and a circuit breaker (same thresholds as the providers') stops trying for 30 s after 5 failures. | SPEC §6: "Qdrant down: the cache is bypassed and requests continue". The breaker keeps a dead Qdrant from adding its timeout to every request. Tested with Qdrant down: 8 requests answered and 5 embeddings tried. | Failing closed, or paying the timeout on every request. |
| **`Cache-Control: no-cache`** forces a fresh answer, which may still be stored; **`no-store`** bypasses the cache both ways. | The standard HTTP header that clients and proxies already understand. | A custom opt-out header. |
| **Collections are created on first use, not at startup**, and the Qdrant client's version check is off. | The gateway must start, and serve uncached, without Qdrant. | Creating the collection at startup: Qdrant becomes a hard dependency. |
| **Embeddings come over the OpenAI `/embeddings` protocol** (Ollama `nomic-embed-text`, 768 dims, by default from the chat upstream). A vector of the wrong size, or a collection built for another size, is reported, not used. | One more integration with no new client. Switching embedding models without dropping the collection would fail every search; the error says what to do. | A Python embedding library inside the gateway: hundreds of MB and CPU on the request path. |

## Configuration

```bash
GATEWAY_CACHE_ENABLED=true
GATEWAY_QDRANT_URL=http://qdrant:6333
GATEWAY_CACHE_EMBEDDING_BASE_URL=          # unset: the chat upstream (Ollama)
GATEWAY_CACHE_EMBEDDING_MODEL=nomic-embed-text
GATEWAY_CACHE_EMBEDDING_DIMS=768
GATEWAY_CACHE_SIMILARITY_THRESHOLD=0.95
GATEWAY_CACHE_TTL_S=3600
GATEWAY_CACHE_TIMEOUT_S=1.0
```

## Known gaps (on purpose)

- **A miss pays for the embedding** (one call to the embedding model, usually tens of ms) before the provider is asked. Lookup and provider could start in parallel, but a hit would then cost an unused model call.
- **Writes don't wait for indexing** (`wait=False`): an identical question asked within milliseconds can still miss.
- **No negative caching or deduplication of in-flight identical questions**: ten identical questions at once all go to the model.
- **The cache is keyed by the requested model**, so a routed name (`smart`) and the model it routes to are different keys.
- **The fake embedder in tests is a bag of words**: it shows the plumbing, not how well a real model judges paraphrases. Thresholds should be tuned on real traffic, with the dashboard's hit-score panel.

## Interview questions to be ready for

1. *How do you stop the cache from answering a different question that only looks similar?*
2. *Why is only the last message embedded, and the rest hashed?*
3. *Qdrant is down. What does a request cost now, and how do you know?*
