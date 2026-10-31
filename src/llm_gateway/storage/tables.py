"""Postgres schema for tenants and API keys (SQLAlchemy Core). The migrations create it."""

from sqlalchemy import Column, DateTime, ForeignKey, MetaData, Table, Text, Uuid, func

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

TENANT_NAME_UNIQUE = "uq_tenants_name"
