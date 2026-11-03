"""The semantic cache's vocabulary: which requests may use it, and what a cached answer is."""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from llm_gateway.chat.model import ChatRequest, Message

# Above this, the provider is sampling: the same question deserves a different answer.
MAX_CACHEABLE_TEMPERATURE = 0.3
# Longer answers aren't kept (and aren't collected while they stream).
MAX_CACHED_ANSWER_CHARS = 64 * 1024


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
    if any(_is_tool_traffic(m) for m in request.messages):
        return None
    *context, last = request.messages
    question = _text_only(last) if last.get("role") == "user" else None
    if not question:
        return None
    return CacheKey(tenant_id, _context_hash(request, context), question)


def _context_hash(request: ChatRequest, context: Sequence[Message]) -> str:
    canonical = json.dumps(
        {"model": request.model, "max_tokens": request.max_tokens, "context": list(context)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


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
