"""Add conversations table and conversation_id to conversation_messages

Revision ID: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("intent_type", sa.Text(), nullable=True),
        sa.Column("topic_summary", sa.Text(), nullable=True),
        sa.Column("thread_id", sa.Text(), nullable=True),
        sa.Column("langfuse_session_id", sa.Text(), nullable=True),
        sa.Column(
            "last_message_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("closed_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("thread_id", name="uq_conversations_thread_id"),
    )
    op.create_index("ix_conversations_user_id", "conversations", ["user_id"])
    op.create_index(
        "ix_conversations_user_status", "conversations", ["user_id", "status"]
    )
    op.create_index(
        "ix_conversations_user_last_msg",
        "conversations",
        ["user_id", "last_message_at"],
    )

    # Add conversation_id to conversation_messages (nullable for backwards compat)
    op.add_column(
        "conversation_messages",
        sa.Column("conversation_id", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_conversation_messages_conversation_id",
        "conversation_messages",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_messages_conversation_id",
        table_name="conversation_messages",
    )
    op.drop_column("conversation_messages", "conversation_id")
    op.drop_index("ix_conversations_user_last_msg", table_name="conversations")
    op.drop_index("ix_conversations_user_status", table_name="conversations")
    op.drop_index("ix_conversations_user_id", table_name="conversations")
    op.drop_table("conversations")
