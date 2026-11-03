"""The tamper-evident part: canonical bytes per event, and a SHA-256 chain per tenant.

    hash_n = SHA-256(hash_(n-1) || canonical(event_n)),   hash_0's predecessor = 64 zeros

Editing, deleting or reordering any stored event breaks every hash after it.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class AuditEvent:
    """Something that happened, already redacted. `details` must be JSON-serializable."""

    tenant_id: UUID
    action: str
    occurred_at: datetime
    details: Mapping[str, Any]


@dataclass(frozen=True)
class ChainHead:
    """The last link of a tenant's chain; seq 0 and GENESIS_HASH before the first event."""

    seq: int
    hash: str


@dataclass(frozen=True)
class AuditRecord:
    tenant_id: UUID
    seq: int
    prev_hash: str
    hash: str
    payload: str  # the canonical JSON that was hashed, stored byte for byte


EMPTY_CHAIN = ChainHead(seq=0, hash=GENESIS_HASH)


def canonical(event: AuditEvent) -> str:
    """One stable text per event: sorted keys, no spaces, UTC ISO time. Hash input and storage."""
    document = {
        "action": event.action,
        "details": event.details,
        "occurred_at": event.occurred_at.isoformat(),
        "tenant_id": str(event.tenant_id),
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def link_hash(prev_hash: str, payload: str) -> str:
    return hashlib.sha256(bytes.fromhex(prev_hash) + payload.encode("utf-8")).hexdigest()


def next_record(head: ChainHead, event: AuditEvent) -> AuditRecord:
    payload = canonical(event)
    return AuditRecord(
        tenant_id=event.tenant_id,
        seq=head.seq + 1,
        prev_hash=head.hash,
        hash=link_hash(head.hash, payload),
        payload=payload,
    )
