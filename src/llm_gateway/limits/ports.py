"""What per-tenant rate limiting and token budgets need from the outside world."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


class RateLimitedError(Exception):
    """The tenant sent requests faster than its rate limit allows."""

    def __init__(self, retry_after_s: float) -> None:
        super().__init__(f"rate limit reached; retry after {retry_after_s:.2f} s")
        self.retry_after_s = retry_after_s


class BudgetExhaustedError(Exception):
    """The tenant's token budget for the current period can't cover this request."""


class LimiterUnavailableError(Exception):
    """The shared rate limiter could not be reached in time."""


class BudgetUnavailableError(Exception):
    """The token budget could not be reached in time."""


@dataclass(frozen=True)
class Reservation:
    """Tokens set aside for one request, settled against actual usage when it ends."""

    tenant_id: UUID
    period: str
    tokens: int


class RateLimiter(Protocol):
    async def acquire(self, tenant_id: UUID) -> None:
        """Take one request slot. Raises RateLimitedError or LimiterUnavailableError."""
        ...


class TokenBudget(Protocol):
    async def reserve(self, tenant_id: UUID, tokens: int) -> Reservation:
        """Set tokens aside atomically.

        Raises BudgetExhaustedError if the period's budget can't cover them, and
        BudgetUnavailableError if the budget store can't be reached.
        """
        ...

    async def settle(self, reservation: Reservation, used_tokens: int) -> None:
        """Replace the reservation with what was actually used (refunds the difference).

        Raises BudgetUnavailableError if the budget store can't be reached.
        """
        ...
