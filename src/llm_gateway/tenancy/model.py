from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class Tenant:
    id: UUID
    name: str


@dataclass(frozen=True)
class StoredKey:
    tenant_id: UUID
    prefix: str
    key_hash: str
