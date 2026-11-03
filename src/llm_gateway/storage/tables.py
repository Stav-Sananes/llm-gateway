"""Postgres schema for tenants and API keys (SQLAlchemy Core). The migrations create it."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    Uuid,
    func,
)

# Named constraints let the store tell which rule a write broke (e.g. a duplicate tenant name).
metadata = MetaData(
    naming_convention={
        "pk": "pk_%(table_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    }
)

tenants = Table(
    "tenants",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("name", Text, nullable=False, unique=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

api_keys = Table(
    "api_keys",
    metadata,
    Column("prefix", Text, primary_key=True),
    # Unique (and so indexed): every authenticated request looks a key up by its hash.
    Column("key_hash", Text, nullable=False, unique=True),
    Column("tenant_id", Uuid, ForeignKey("tenants.id"), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    # Revoked keys are kept, so audit entries can still be traced to their tenant.
    Column("revoked_at", DateTime(timezone=True)),
)

# One row per tenant: the last link. Appends lock it, so a tenant's chain stays a line.
audit_heads = Table(
    "audit_heads",
    metadata,
    Column("tenant_id", Uuid, ForeignKey("tenants.id"), primary_key=True),
    Column("seq", BigInteger, nullable=False),
    Column("hash", Text, nullable=False),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column("tenant_id", Uuid, ForeignKey("tenants.id"), nullable=False),
    Column("seq", BigInteger, nullable=False),
    Column("prev_hash", Text, nullable=False),
    Column("hash", Text, nullable=False),
    # TEXT, not JSONB: JSONB would reorder keys, and the stored bytes must be what was hashed.
    Column("payload", Text, nullable=False),
    Column("recorded_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    PrimaryKeyConstraint("tenant_id", "seq"),
    # verify starts at seq 1; rows below it could never be checked, so they can't exist.
    CheckConstraint("seq > 0", name="ck_audit_events_seq_positive"),
)

TENANT_NAME_UNIQUE = "uq_tenants_name"
