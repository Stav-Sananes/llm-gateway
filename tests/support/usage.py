"""Fakes for the usage ports, and a usage event builder."""

import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from llm_gateway.usage.model import Outcome, UsageEvent
from llm_gateway.usage.ports import (
    Delivery,
    InvalidUsageEventError,
    UsageQueueUnavailableError,
    UsageStoreUnavailableError,
)


class RecordedUsage:
    def __init__(self) -> None:
        self.events: list[UsageEvent] = []

    async def record(self, event: UsageEvent) -> None:
        self.events.append(event)


class FakeClock:
    """Each reading is `step_s` after the previous one."""

    def __init__(self, step_s: float = 1.0) -> None:
        self._now = 0.0
        self._step_s = step_s

    def __call__(self) -> float:
        self._now += self._step_s
        return self._now


def a_usage_event(**changes: object) -> UsageEvent:
    event = UsageEvent(
        event_id=uuid.uuid4(),
        tenant_id=UUID(int=1),
        requested_model="smart",
        provider="ollama",
        model="llama3",
        outcome=Outcome.COMPLETED,
        prompt_tokens=7,
        completion_tokens=5,
        charged_tokens=12,
        started_at=datetime(2026, 11, 3, 12, 0, tzinfo=UTC),
        duration_s=1.5,
        time_to_first_chunk_s=0.25,
    )
    return replace(event, **changes)  # type: ignore[arg-type]


class InMemoryUsageQueue:
    """Deliveries stay pending until acknowledged, as in a Redis consumer group."""

    def __init__(self, *deliveries: Delivery, down: bool = False) -> None:
        self.pending: list[Delivery] = list(deliveries)
        self.acked: list[str] = []
        self.dead: list[Delivery] = []
        self.down = down

    async def take(self, max_events: int) -> Sequence[Delivery]:
        if self.down:
            raise UsageQueueUnavailableError("redis down")
        return [d for d in self.pending if d.delivery_id not in self.acked][:max_events]

    async def ack(self, delivery_ids: Sequence[str]) -> None:
        self.acked.extend(delivery_ids)

    async def dead_letter(self, delivery: Delivery) -> None:
        self.dead.append(delivery)
        await self.ack([delivery.delivery_id])


class InMemoryUsageStore:
    """Refuses events with negative tokens, as the Postgres check constraint does."""

    def __init__(self, down: bool = False) -> None:
        self.events: dict[UUID, UsageEvent] = {}
        self.down = down

    async def insert_many(self, events: Sequence[UsageEvent]) -> None:
        if self.down:
            raise UsageStoreUnavailableError("postgres down")
        if any(e.prompt_tokens < 0 or e.charged_tokens < 0 for e in events):
            raise InvalidUsageEventError("ck_usage_events_tokens_not_negative")
        for event in events:
            self.events.setdefault(event.event_id, event)
