"""Add service_heartbeat table

Revision ID: 0002
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "service_heartbeat",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("last_alive_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("shutdown_type", sa.Text(), nullable=False, server_default="crash"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("service_heartbeat")
