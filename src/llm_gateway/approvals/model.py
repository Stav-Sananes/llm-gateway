"""A tool call parked until a human approves or rejects it."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

GATEWAY = "gateway"  # decided_by when the gateway closed it, not a human


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"  # a human approved: the waiting call runs
    REJECTED = "rejected"  # a human rejected: the call never runs
    EXPIRED = "expired"  # nobody decided in time
    ABANDONED = "abandoned"  # the client stopped waiting (or the gateway failed) first


@dataclass(frozen=True)
class Approval:
    id: UUID
    tenant_id: UUID
    tenant_name: str
    tool: str  # server__tool
    arguments: Mapping[str, Any]  # exactly what will run if approved
    reasons: tuple[str, ...]  # the policies that asked for approval
    status: ApprovalStatus
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    note: str | None = None
