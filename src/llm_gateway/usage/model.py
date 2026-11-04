"""One usage event per provider a chat request used: who asked, who answered, what it cost.

Usually one per request; a request resumed mid-answer has one per provider that served it,
with the same tenant and started_at."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class Outcome(StrEnum):
    COMPLETED = "completed"  # the provider reported usage: the answer finished
    RESUMED = "resumed"  # the provider died mid-answer and another one continued it
    INTERRUPTED = "interrupted"  # output came back, then the stream broke or the client left
    FAILED = "failed"  # nothing came back: every provider was unavailable


@dataclass(frozen=True)
class UsageEvent:
    event_id: UUID  # idempotency key: the worker may deliver an event more than once
    tenant_id: UUID
    requested_model: str
    provider: str | None  # None when no provider served anything
    model: str | None
    outcome: Outcome
    prompt_tokens: int
    completion_tokens: int
    charged_tokens: int  # what the budget was settled at (the reservation when unreported)
    started_at: datetime
    duration_s: float
    time_to_first_chunk_s: float | None
