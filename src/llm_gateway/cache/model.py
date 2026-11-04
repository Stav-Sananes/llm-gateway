"""The semantic cache's vocabulary: which requests may use it, and what a cached answer is."""

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from llm_gateway.chat.model import ChatRequest, Message

# Above this, the provider is sampling: the same question deserves a different answer.
MAX_CACHEABLE_TEMPERATURE = 0.3
# Longer answers aren't kept (and aren't collected while they stream).
MAX_CACHED_ANSWER_CHARS = 64 * 1024
# Longer questions match only word for word. An embedding of a long text is dominated by
# its bulk, and past the model's context the tail is cut off: a shared document followed
# by different instructions scores 1.0.
MAX_SEMANTIC_QUESTION_CHARS = 1000
# A cached answer is reused only if the questions also agree word for word this much.
MIN_WORD_OVERLAP = 0.8
# ChatRequest fields that change the answer, all in the context hash. A test fails when
# ChatRequest gains a field that isn't here (or explicitly excluded).
KEYED_REQUEST_FIELDS = frozenset({"model", "max_tokens", "temperature", "has_tools"})


@dataclass(frozen=True)
class CacheDirectives:
    """What the client allows, from its Cache-Control header."""

    read: bool = True  # "no-cache": don't answer from the cache (a fresh answer may be stored)
    write: bool = True  # "no-store": don't keep this answer (and don't read either)

    @classmethod
    def from_header(cls, cache_control: str | None) -> "CacheDirectives":
        directives = {d.strip().lower() for d in (cache_control or "").split(",")}
        no_store = "no-store" in directives
        return cls(read="no-cache" not in directives and not no_store, write=not no_store)


DEFAULT_DIRECTIVES = CacheDirectives()


@dataclass(frozen=True)
class CacheKey:
    """Everything but the last question must match exactly; the question matches by meaning.

    A similar question in a different conversation (system prompt, earlier turns, model,
    max_tokens) is a different question, so the context is hashed, not embedded."""

    tenant_id: UUID
    context_hash: str
    question: str


@dataclass(frozen=True)
class CachedAnswer:
    content: str
    finish_reason: str
    model: str  # the model that originally answered


def cache_key(tenant_id: UUID, request: ChatRequest) -> CacheKey | None:
    """The request's key, or None when it must not use the cache at all."""
    if request.has_tools or request.temperature is None:
        return None
    if request.temperature > MAX_CACHEABLE_TEMPERATURE:
        return None
    if any(_is_tool_traffic(m) or _text_only(m) is None for m in request.messages):
        return None  # tool traffic, or anything but text anywhere (an image changes meaning)
    *context, last = request.messages
    question = _text_only(last)
    if last.get("role") != "user" or not question:
        return None
    return CacheKey(tenant_id, _context_hash(request, context, last, question), question)


def lexically_compatible(question: str, cached_question: str) -> bool:
    """A guard on top of similarity: embeddings place "safe" next to "unsafe" and "2" next
    to "3". Numbers and negations must match exactly, no word may flip to its opposite
    (un-/non-/in-/dis- forms), and most words must be shared."""
    words, cached = _words(question), _words(cached_question)
    if {w for w in words if w.isdigit()} != {w for w in cached if w.isdigit()}:
        return False
    if _negations(words) != _negations(cached) or _flipped(words ^ cached, words | cached):
        return False
    union = words | cached
    return bool(union) and len(words & cached) / len(union) >= MIN_WORD_OVERLAP


def _context_hash(
    request: ChatRequest, context: Sequence[Message], last: Message, question: str
) -> str:
    keyed = {name: getattr(request, name) for name in sorted(KEYED_REQUEST_FIELDS)}
    canonical = json.dumps(
        {
            "request": keyed,
            "context": list(context),
            # Everything about the question except its text (a name, say)...
            "last": {k: v for k, v in last.items() if k != "content"},
            # ...and the text itself when it's too long to match by meaning.
            "long_question": question if len(question) > MAX_SEMANTIC_QUESTION_CHARS else None,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_NEGATING_PREFIXES = ("un", "non", "in", "im", "ir", "il", "dis")
_NEGATIONS = frozenset(
    {"no", "not", "never", "none", "nothing", "nobody", "neither", "nor", "without", "cannot"}
)


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _negations(words: set[str]) -> set[str]:
    return {w for w in words if w in _NEGATIONS or w.endswith("n't")}


def _flipped(differing: set[str], all_words: set[str]) -> bool:
    """Does a word one question has and the other lacks negate a word either one has?"""
    return any(
        word.startswith(prefix) and word.removeprefix(prefix) in all_words
        for word in differing
        for prefix in _NEGATING_PREFIXES
    )


def _is_tool_traffic(message: Message) -> bool:
    return message.get("role") in {"tool", "function"} or bool(message.get("tool_calls"))


def _text_only(message: Message) -> str | None:
    """The message's text, or None if it carries anything else (an image changes the
    question in ways its text doesn't show)."""
    content: Any = message.get("content")
    if isinstance(content, str):
        return content.strip() or None
    if not isinstance(content, list):
        return None
    parts = [p for p in content if isinstance(p, Mapping)]
    if len(parts) != len(content) or any(p.get("type") != "text" for p in parts):
        return None
    return "\n".join(str(p.get("text", "")) for p in parts).strip() or None
