"""Create usage_events: one row per chat request, written by the usage worker.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-03
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("requested_model", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("charged_tokens", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_s", sa.Float(), nullable=False),
        sa.Column("time_to_first_chunk_s", sa.Float(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_usage_events"),
        sa.CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND charged_tokens >= 0",
            name="ck_usage_events_tokens_not_negative",
        ),
    )
    op.create_index(
        "ix_usage_events_tenant_id_started_at", "usage_events", ["tenant_id", "started_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_usage_events_tenant_id_started_at", table_name="usage_events")
    op.drop_table("usage_events")
