# 11 — Mid-stream resume

**Goal:** when a provider dies after it has started answering, the next provider on the route continues the same answer. The client sees one unbroken stream. Inside the gateway the switch is recorded: one usage row per provider, an audit record, and an OTel span.

This is build step 8.

## How a resume happens

```
Router.stream("smart") → targets [anthropic/claude-opus-5-5, local/qwen2.5:0.5b]
  anthropic streams "Hello", " from" … then an overloaded error: ProviderUnavailableError
     (before any finish_reason; after one, the answer is whole and nothing is resumed)
  continuation(request, sent="Hello from"):
     messages + {"role": "assistant", "content": "Hello from"}
              + {"role": "user", "content": CONTINUE_INSTRUCTION}
     max_tokens − estimate("Hello from")      (none left → finish_reason "length", no call)
  local streams "Hello from the fake LLM."   (its first chunk carries Origin(local, qwen))
  Seam: the continuation's opening repeats the end of the answer → drop it → " the fake LLM."
client: "Hello from the fake LLM."   one stream, same id, model "smart"
finish (shielded): usage rows [anthropic: resumed, local: completed]; the switch is queued
background writer: audit chat.provider_switched on the tenant's chain
```

## Decisions

| Decision | Why | Rejected alternative |
|---|---|---|
| **Ask for the continuation in words, not with assistant prefill.** The partial answer goes back as an assistant turn, followed by a user turn: continue exactly where it stopped, don't repeat, don't mention the interruption. *(Stav's decision, 2026-11-05.)* | The spec said prefill, but current Claude models return 400 on a prefilled assistant turn. Asking in words works on every provider, Claude and Ollama alike. | Prefill only where it's supported: no resume whenever Claude is the fallback. Ending the stream with an error and letting the client retry: honest, but not what the spec asks for. |
| **A seam drops the continuation's opening when it repeats the end of the answer** (≥ 8 characters, looked for in the last 2,000). While the continuation could still turn out to be a repeat, its text is held back; as soon as it can't, the text is released, minus the repeat. | Models told not to repeat still often repeat the last words, or start over. Shorter repeats are too likely to be real text ("the", "a "). Holding only while it could be a repeat keeps the delay to a few chunks. | No trimming: the client sees "Hello fromHello from …". Asking the model to mark where it resumes: one more instruction to ignore. |
| **Only `ProviderUnavailableError` before a finish chunk triggers a resume. A target that refuses the continuation (4xx) is skipped like a dead one.** When no target is left, the stream ends with an in-band error event. A stream that broke after its finish chunk is left as it was. | A dead connection, a read timeout between tokens or an overloaded event means "try elsewhere". The client's request was already accepted once, so a 4xx on the continuation is about the continuation (the partial answer pushing it past that model's context, say), and another model may take it. After a finish chunk the answer is whole; only its usage report was lost. | Ending the answer at the first refusal. Retrying a refused *original* request elsewhere: that one is wrong everywhere, and is still not retried. |
| **A partial answer of only whitespace is not resumed; the next target gets the original request.** | Some APIs refuse an assistant turn that is only whitespace, and there is nothing to continue. | Sending `"\n"` back as the assistant turn. |
| **A provider's call counts as a success for its circuit breaker only when its stream ends cleanly**, not at its first chunk. | Resume hides a provider that dies mid-answer every time. Counted at the first chunk, each success reset the failures and the circuit never opened, so every request paid for a resume. The cost: a half-open trial now holds its slot until the whole answer is streamed. | Counting mid-answer deaths in a second, separate bucket. |
| **The continuation's `max_tokens` is what's left** after the over-estimated tokens of the partial answer (bytes / 3). | The client's cap holds for the whole answer. Over-estimating can end a resumed English answer up to about a quarter early; it never runs past the cap. | The original `max_tokens` again: the answer could be up to twice as long as asked. |
| **The router does it; the use case meters it.** The router knows the targets and the stream. The first chunk each target serves carries its `Origin`, so a new `Origin` mid-stream tells the use case that another provider took over. | Resume needs no new port between router and use case, and the stream stays the only channel. | The router calling the usage and audit ports itself: routing would depend on tenants and metering. |
| **One usage row per provider.** The provider that died gets `outcome = resumed`, charged the estimate of its prompt (plus the answer it was asked to continue, for a second or later one) and of the text it streamed. The one that finished is charged its reported usage. If the answer ends with no usage report at all, the request is charged the rest of its reservation, as before. The rows share `tenant_id` and `started_at`. | The tenant pays for what each provider did. A dead provider never reports usage, so its cost can only be estimated, and the estimate errs high (bytes / 3). A resumed request can cost more than its reservation (the continuation's prompt carries the partial answer); the settle charges what was used. | One row with the last provider: hides that two models answered. Charging the dead provider nothing: a provider that always dies late would give free tokens. |
| **The switch goes on the tenant's audit chain** (`chat.provider_switched`, with the requested model and both provider/model pairs) **through a bounded background queue** (100), like the cache writer. A full queue or a down audit store loses the record, logged as an error; the request isn't failed. | The answer has already been served, so failing it would protect nothing. In a provider outage every in-flight stream resumes at once: appending inline would hold each `[DONE]` behind a per-tenant row lock and compete with tool-call audits, which must happen before their action. | Auditing inline in the shielded finish (the first version). Auditing at the seam, before the continuation's first chunk: one Postgres round trip in the middle of the stream. |
| **A resume is counted once as a request and once as a resume.** `gateway_chat_requests_total` counts the request under the provider that finished it. `gateway_chat_resumes_total{provider}` counts the providers that died mid-answer. | One usage event per provider would otherwise count a resumed request twice, and request totals and error rates would drift. | A `resumed` outcome label on the request counter. |
| **A `gateway.resume` span** with both targets and the partial answer's length, next to the two providers' own `chat` spans. | One trace shows where an answer changed hands, without reading the usage rows. | Span events on the HTTP span: the router doesn't own that span. |
| **A stitched answer is never stored in the semantic cache.** | It's the product of two models and an instruction. Replaying it as one model's answer would hide that. | Caching it like any other answer. |

## Checked against real models

A scratch script cut real answers at 45 % and asked Ollama to continue them through `continuation()` and the `Seam`. The models ran at temperature 0, on 8 questions (prose, a list, a haiku, arithmetic steps, code).

| | `qwen2.5:3b` with this instruction | `qwen2.5:3b`, instruction quoting the answer's last 80 characters |
|---|---|---|
| Joined cleanly | **4 of 8**, and all four had started over: the seam dropped 31–394 repeated characters ("data acc" + "ess and processing") | 1 of 8 |
| Failures | a repeat shorter than 8 characters ("Ve" + "Ve\nVenus"); "Sure, let's continue …"; the instruction itself echoed back; a word left unfinished ("bel" + "down") | garbled lists, "returnreturn", answers ended early |

`qwen2.5:0.5b` failed with both instructions, so it can't tell them apart. Larger models should do better, but that was not measured. The plain instruction stays, and the seam is what makes it work: models mostly start over rather than continue.

## Interview pressure point: one answer from two models, silently

The client is never told. The defense: it's a deliberate availability-first choice, and it's recorded where operators look (usage rows with `outcome = resumed`, the audit chain, the trace). The case against: the second model may contradict the first, change style mid-sentence or repeat itself, and a client that compares models or evaluates them can't know. A gateway for regulated tenants might expose it instead, for example with a header or a per-tenant switch to fail instead of resume. That's a small change on top of this one, because the switch is already detected in one place.

## Known gaps

- **The seam only catches literal repeats.** A continuation that rephrases the last sentence, or that starts the whole answer over when the answer is longer than 2,000 characters, reaches the client repeated. So do the measured failure modes above: meta-talk ("Sure, let's continue"), an echoed instruction, short repeats.
- **Tool calls are not resumed.** The chat path streams text only; a request with tools that dies mid-answer is resumed as text.
- **The dead provider's tokens are estimated** from the prompt and its own visible text. Output the client never saw (thinking, text held at a seam when that continuation died too) isn't counted. Before resume, a death mid-answer was charged the whole reservation.
- **The usage the client sees is only the last provider's report**: its prompt includes the partial answer, and its completion covers only the continuation.
- **An answer that used up `max_tokens` before its provider died** ends with a `length` finish and no new call. That provider's row says `interrupted` and is charged the rest of the reservation, which is about what it used.
- **Deploy the usage worker before the gateway:** an older worker can't decode `outcome = resumed` and dead-letters those events.
- **Usage rows of one request are linked only by tenant and `started_at`.** A request id column would make the join exact; it needs a migration.
- **No limit beyond the route:** an answer can change hands as many times as the route has targets.

## Interview questions to be ready for

1. *Why not just retry the whole request on the fallback?*
2. *The fallback repeats the last sentence. What does the client see, and why?*
3. *Who pays for the tokens the dead provider generated, and how do you know how many there were?*
