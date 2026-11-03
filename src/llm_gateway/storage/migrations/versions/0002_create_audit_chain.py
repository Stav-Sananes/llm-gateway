"""Create the per-tenant audit hash chain.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-03
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "audit_heads",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_audit_heads_tenant_id_tenants"
        ),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_audit_heads"),
    )
    op.create_table(
        "audit_events",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("prev_hash", sa.Text(), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_audit_events_tenant_id_tenants"
        ),
        sa.PrimaryKeyConstraint("tenant_id", "seq", name="pk_audit_events"),
        sa.CheckConstraint("seq > 0", name="ck_audit_events_seq_positive"),
    )


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("audit_heads")
