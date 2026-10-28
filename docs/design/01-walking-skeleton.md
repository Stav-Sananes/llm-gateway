# 01 — Walking skeleton

**Goal:** the thinnest slice that works end to end. A client sends an OpenAI request, the gateway streams it through Ollama, and the response comes back traced, built, tested and running in CI. Every later step plugs into this shape.

## Request path

```
POST /v1/chat/completions
  → http/schemas.py      validate the body (pydantic) → domain ChatRequest
  → chat/use_case.py     ChatCompletions.stream()  (the seam where auth, limits and routing land later)
  → providers/openai_compatible.py   httpx stream → SSE lines → CompletionChunk
  → http/presenter.py    CompletionChunk → OpenAI chunk JSON → SSE
```

`main.py` is the only module that knows concrete classes (the composition root). `chat/` imports nothing from FastAPI, httpx or OpenTelemetry.

## Decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| **Always stream upstream**, even when the client asked for a single response. Non-streaming clients get the chunks folded together by `collect()`. | One code path for metering, caching and fallback. We always get usage numbers (`stream_options.include_usage`). | Two code paths. Every later feature would have to be built twice. |
| **Domain chunk type** (`CompletionChunk`) instead of passing upstream JSON through untouched. | The Anthropic adapter (step 4) has to translate formats anyway. The budget and resume features need parsed tokens and usage. | Raw pass-through. Faster to write, but the provider format would leak everywhere. |
| **Prime the stream:** read the first chunk *before* sending the HTTP headers. | Upstream failures that happen up front (connect error, 4xx, 5xx) still produce a real status code (502, 404). | Start the response immediately. Every failure would turn into a 200 carrying an error event. |
| **In-band error event** once the stream has started. | The headers are already sent, so the status can't change. OpenAI does the same. | Silently cutting the connection. Clients couldn't tell what happened. |
| **Port returns an `AsyncGenerator`**, not an `AsyncIterator`. | When the client disconnects, the presenter can call `aclose()`, which releases the upstream connection immediately (Release It: Steady State). | An iterator. The connection would stay open until garbage collection. |
| **Explicit timeouts:** connect 2 s, read 60 s, measured *per chunk*. | The read timeout limits the gap *between tokens*, not the total generation time. Long answers are fine; a stalled stream fails. | httpx's default of 5 s total for everything. Long generations would be killed. |
| **The provider span is started detached** (not set as the current span). | The generator is resumed from different asyncio tasks, and attaching context across `yield` corrupts it. | `start_as_current_span` around the generator. Context errors under load. |
| **`OpenAICompatibleProvider`** is named after the protocol, not after Ollama. | The same adapter serves Ollama, vLLM, LM Studio and others. | `OllamaProvider`. A misleading name. |
| **Tests run on real sockets** against a fake LLM we own (`harness/fake_llm.py`). | Exercises real chunked HTTP streaming and real dropped connections. "Mock only types you own." The same fake drives the k6 load tests in step 10. | Mocking httpx. That tests our mocks, not streaming. |

## Known gaps (on purpose)

- Tool calls inside streamed deltas aren't parsed yet; they come with step 4 or 5.
- There's one upstream and no routing. The router arrives with fallback in step 4.
- There's no auth (step 2).

## Interview questions to be ready for

1. *Why can't the gateway return a 502 when the provider dies halfway through a streamed answer? What does the client see instead?*
2. *The read timeout is 60 s. Does that mean a 3-minute generation fails? Why not?*
3. *Why does the gateway stream from the provider even when the client asked for a non-streaming response? What does that cost?*
