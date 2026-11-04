"""Postgres schema (SQLAlchemy Core). The migrations create it; a test checks they agree."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
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

# No foreign key to tenants: an event the worker can't insert would block the queue behind
# it. Usage is analytics; the budget in Redis is what enforces limits.
usage_events = Table(
    "usage_events",
    metadata,
    Column("event_id", Uuid, primary_key=True),  # the worker may deliver an event twice
    Column("tenant_id", Uuid, nullable=False),
    Column("requested_model", Text, nullable=False),
    Column("provider", Text),
    Column("model", Text),
    Column("outcome", Text, nullable=False),
    Column("prompt_tokens", BigInteger, nullable=False),
    Column("completion_tokens", BigInteger, nullable=False),
    Column("charged_tokens", BigInteger, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("duration_s", Float, nullable=False),
    Column("time_to_first_chunk_s", Float),
    Column("recorded_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    CheckConstraint(
        "prompt_tokens >= 0 AND completion_tokens >= 0 AND charged_tokens >= 0",
        name="ck_usage_events_tokens_not_negative",
    ),
    # Per-tenant usage over a time range: the dashboard's and billing's question.
    Index("ix_usage_events_tenant_id_started_at", "tenant_id", "started_at"),
)

# Tool calls a policy held for a human. The arguments are kept exactly (as JSON text):
# they are what the human approves, and what runs.
approvals = Table(
    "approvals",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("tenant_id", Uuid, ForeignKey("tenants.id"), nullable=False),
    Column("tenant_name", Text, nullable=False),
    Column("tool", Text, nullable=False),
    Column("arguments", Text, nullable=False),
    Column("reasons", Text, nullable=False),  # a JSON list of policy ids
    Column("status", Text, nullable=False),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("decided_at", DateTime(timezone=True)),
    Column("decided_by", Text),
    Column("note", Text),
    CheckConstraint(
        "status IN ('pending', 'approved', 'rejected', 'expired', 'abandoned')",
        name="ck_approvals_status_known",
    ),
    # What's waiting, per tenant: the hold's limit and the admin's list.
    Index("ix_approvals_status_tenant_id", "status", "tenant_id"),
)

TENANT_NAME_UNIQUE = "uq_tenants_name"
