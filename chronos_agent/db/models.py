from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Integer,
    LargeBinary,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    user_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)

    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="UTC")

    gcal_refresh_token: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending_oauth")

    gcal_events_channel_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    gcal_events_resource_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    gcal_events_channel_expiry: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    gcal_tasks_channel_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    gcal_tasks_resource_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    gcal_tasks_channel_expiry: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class CalendarEvent(Base):
    __tablename__ = "calendar_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    user_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    gcal_event_id: Mapped[str] = mapped_column(Text, nullable=False)

    calendar_id: Mapped[str] = mapped_column(Text, nullable=False, default="primary")

    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)

    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    is_all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    status: Mapped[str] = mapped_column(Text, nullable=False, default="confirmed")

    recurrence: Mapped[str | None] = mapped_column(Text, nullable=True)

    raw_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "gcal_event_id", name="uq_calendar_events_user_gcal"),
        Index("ix_calendar_events_user_start", "user_id", "start_at"),
    )


class CalendarTask(Base):
    __tablename__ = "calendar_tasks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    user_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    gcal_task_id: Mapped[str] = mapped_column(Text, nullable=False)

    tasklist_id: Mapped[str] = mapped_column(Text, nullable=False, default="@default")

    title: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str] = mapped_column(Text, nullable=False, default="needsAction")

    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    raw_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "gcal_task_id", name="uq_calendar_tasks_user_gcal"),
        Index("ix_calendar_tasks_user_status_due", "user_id", "status", "due_at"),
    )


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    status: Mapped[str] = mapped_column(Text, nullable=False, default="active")

    intent_type: Mapped[str | None] = mapped_column(Text, nullable=True)

    topic_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    thread_id: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True)

    langfuse_session_id: Mapped[str | None] = mapped_column(Text, nullable=True)

    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_conversations_user_status", "user_id", "status"),
        Index("ix_conversations_user_last_msg", "user_id", "last_message_at"),
    )


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    user_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)

    conversation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (Index("ix_conversation_messages_user_created", "user_id", "created_at"),)


class ServiceHeartbeat(Base):
    __tablename__ = "service_heartbeat"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    last_alive_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    shutdown_type: Mapped[str] = mapped_column(Text, nullable=False, default="crash")


class ServiceLog(Base):
    __tablename__ = "service_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    level: Mapped[str] = mapped_column(Text, nullable=False)

    event: Mapped[str] = mapped_column(Text, nullable=False)

    user_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    extra: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    agent_version: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_service_logs_timestamp", "timestamp"),
        Index("ix_service_logs_user_id", "user_id"),
    )
