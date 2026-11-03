"""Ports for usage events: emitting them on the request path, and draining them to storage."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from llm_gateway.usage.model import UsageEvent


class UsageRecorder(Protocol):
    """A notification: must not raise and must return quickly. Metering is not worth
    failing, or slowing, the request it measures."""

    async def record(self, event: UsageEvent) -> None: ...


class NoUsageRecording:
    async def record(self, event: UsageEvent) -> None:
        pass


@dataclass(frozen=True)
class Delivery:
    """One queued event. `event` is None when the message couldn't be decoded; `raw` is
    the message as queued, kept for the dead-letter queue."""

    delivery_id: str
    event: UsageEvent | None
    raw: str = ""

    @property
    def lost(self) -> bool:
        """The entry was trimmed from the stream before it was stored: nothing to keep."""
        return self.event is None and not self.raw


class UsageQueue(Protocol):
    async def take(self, max_events: int) -> Sequence[Delivery]:
        """Deliveries never acknowledged for a while (a crashed worker's), then new ones."""
        ...

    async def ack(self, delivery_ids: Sequence[str]) -> None: ...

    async def dead_letter(self, delivery: Delivery) -> None:
        """Park an undecodable delivery for a human and acknowledge it."""
        ...


class UsageStore(Protocol):
    async def insert_many(self, events: Sequence[UsageEvent]) -> None:
        """All or nothing, idempotent on event_id. Raises UsageStoreUnavailableError, or
        InvalidUsageEventError when the store refuses the data itself."""
        ...

    async def reachable(self) -> bool:
        """Does the store answer at all? Tells a refused batch from a down store."""
        ...


class UsageQueueUnavailableError(Exception):
    """The usage queue (Redis) could not be reached."""


class UsageStoreUnavailableError(Exception):
    """The usage store (Postgres) could not be reached."""


class InvalidUsageEventError(Exception):
    """The store refused an event's data (a constraint), not the connection."""
