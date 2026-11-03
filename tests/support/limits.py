"""Fakes for the limits ports, and a scripted chat provider."""

from collections.abc import Sequence
from typing import NoReturn
from uuid import UUID

from llm_gateway.chat.model import ChatRequest, CompletionChunk
from llm_gateway.chat.ports import ChunkStream, ProviderUnavailableError
from llm_gateway.limits.ports import (
    BudgetExhaustedError,
    BudgetUnavailableError,
    LimiterUnavailableError,
    Reservation,
)


class InMemoryTokenBudget:
    """Same rules as the Redis budget, with a single period."""

    def __init__(self, tokens_per_period: int) -> None:
        self._limit = tokens_per_period
        self._used: dict[UUID, int] = {}
        self.settled: list[int] = []  # used_tokens of each settle, in order

    async def reserve(self, tenant_id: UUID, tokens: int) -> Reservation:
        used = self._used.get(tenant_id, 0)
        if used + tokens > self._limit:
            raise BudgetExhaustedError(f"tenant {tenant_id} is out of tokens")
        self._used[tenant_id] = used + tokens
        return Reservation(tenant_id=tenant_id, period="today", tokens=tokens)

    async def settle(self, reservation: Reservation, used_tokens: int) -> None:
        self.settled.append(used_tokens)
        if reservation.tenant_id in self._used:
            self._used[reservation.tenant_id] += used_tokens - reservation.tokens

    def used(self, tenant_id: UUID) -> int:
        return self._used.get(tenant_id, 0)


class UnreachableRateLimiter:
    async def acquire(self, tenant_id: UUID) -> NoReturn:
        raise LimiterUnavailableError("rate limiter is down")


class SettleFailsBudget(InMemoryTokenBudget):
    """Reserves fine, then loses its store before the request ends."""

    async def settle(self, reservation: Reservation, used_tokens: int) -> NoReturn:
        raise BudgetUnavailableError("budget store went down")


class ScriptedProvider:
    """Streams the given chunks; optionally fails after them, like a dropped upstream."""

    def __init__(self, chunks: Sequence[CompletionChunk], fail_after: bool = False) -> None:
        self._chunks = chunks
        self._fail_after = fail_after
        self.requests: list[ChatRequest] = []
        self.closed = False  # the upstream connection was released

    async def stream(self, request: ChatRequest) -> ChunkStream:
        self.requests.append(request)
        try:
            for chunk in self._chunks:
                yield chunk
            if self._fail_after:
                raise ProviderUnavailableError("upstream dropped the stream")
        finally:
            self.closed = True
