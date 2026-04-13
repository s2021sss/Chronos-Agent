"""Initial schema: users, calendar_events, calendar_tasks, service_logs

Revision ID: 0001
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -----------------------------------------------------------------------
    # users
    # -----------------------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False, server_default="UTC"),
        sa.Column("gcal_refresh_token", sa.LargeBinary(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending_oauth"),

        sa.Column("gcal_events_channel_id", sa.Text(), nullable=True),
        sa.Column("gcal_events_resource_id", sa.Text(), nullable=True),
        sa.Column(
            "gcal_events_channel_expiry",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("gcal_tasks_channel_id", sa.Text(), nullable=True),
        sa.Column("gcal_tasks_resource_id", sa.Text(), nullable=True),
        sa.Column(
            "gcal_tasks_channel_expiry",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_user_id", "users", ["user_id"], unique=True)

    # -----------------------------------------------------------------------
    # calendar_events
    # -----------------------------------------------------------------------
    op.create_table(
        "calendar_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("gcal_event_id", sa.Text(), nullable=False),
        sa.Column("calendar_id", sa.Text(), nullable=False, server_default="primary"),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_all_day", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("status", sa.Text(), nullable=False, server_default="confirmed"),
        sa.Column("recurrence", sa.Text(), nullable=True),
        sa.Column("raw_json", postgresql.JSONB(), nullable=True),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "gcal_event_id", name="uq_calendar_events_user_gcal"
        ),
    )
    op.create_index(
        "ix_calendar_events_user_id", "calendar_events", ["user_id"], unique=False
    )
    op.create_index(
        "ix_calendar_events_user_start",
        "calendar_events",
        ["user_id", "start_at"],
        unique=False,
    )

    # -----------------------------------------------------------------------
    # calendar_tasks
    # -----------------------------------------------------------------------
    op.create_table(
        "calendar_tasks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("gcal_task_id", sa.Text(), nullable=False),
        sa.Column("tasklist_id", sa.Text(), nullable=False, server_default="@default"),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="needsAction"),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("raw_json", postgresql.JSONB(), nullable=True),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "gcal_task_id", name="uq_calendar_tasks_user_gcal"
        ),
    )
    op.create_index(
        "ix_calendar_tasks_user_id", "calendar_tasks", ["user_id"], unique=False
    )
    op.create_index(
        "ix_calendar_tasks_user_status_due",
        "calendar_tasks",
        ["user_id", "status", "due_at"],
        unique=False,
    )

    # -----------------------------------------------------------------------
    # service_logs
    # -----------------------------------------------------------------------
    op.create_table(
        "service_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("extra", postgresql.JSONB(), nullable=True),
        sa.Column("agent_version", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_service_logs_timestamp", "service_logs", ["timestamp"], unique=False
    )
    op.create_index(
        "ix_service_logs_user_id", "service_logs", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_table("service_logs")
    op.drop_table("calendar_tasks")
    op.drop_table("calendar_events")
    op.drop_table("users")
