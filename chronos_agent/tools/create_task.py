"""
create_task — создание задачи в Google Tasks + локальная БД.

Идемпотентность:
  Проверяет наличие задачи с тем же user_id, title и due_date ±1 день.
  При совпадении возвращает существующий gcal_task_id.

Санация:
  title и notes очищаются через sanitize.py (Pydantic validator).
"""

import asyncio
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, field_validator
from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from chronos_agent.db.engine import get_session
from chronos_agent.db.models import CalendarTask
from chronos_agent.google.client import build_tasks_service
from chronos_agent.logging import get_logger
from chronos_agent.tools._helpers import get_user_token
from chronos_agent.tools.retry import call_with_retry
from chronos_agent.tools.sanitize import sanitize_text, sanitize_title

logger = get_logger(__name__)

_IDEMPOTENCY_WINDOW = timedelta(days=1)


class CreateTaskInput(BaseModel):
    user_id: str
    title: str
    due_date: datetime | None = None
    notes: str | None = None

    @field_validator("title")
    @classmethod
    def _sanitize_title(cls, v: str) -> str:
        return sanitize_title(v)

    @field_validator("notes")
    @classmethod
    def _sanitize_notes(cls, v: str | None) -> str | None:
        return sanitize_text(v)

    model_config = {"arbitrary_types_allowed": True}


async def create_task(inp: CreateTaskInput) -> str:
    """
    Создаёт задачу в Google Tasks и локальной БД.
    Возвращает gcal_task_id.
    """
    # Idempotency check
    existing_id = await _find_duplicate_task(inp)
    if existing_id:
        logger.info("create_task_duplicate", user_id=inp.user_id, gcal_task_id=existing_id)
        return existing_id

    # Создание в Google Tasks
    encrypted_token = await get_user_token(inp.user_id)
    gcal_task_id = await asyncio.to_thread(_create_in_google, inp.user_id, encrypted_token, inp)

    # Сохранение в локальную БД
    await _upsert_task(inp, gcal_task_id)

    logger.info("create_task_done", user_id=inp.user_id, gcal_task_id=gcal_task_id, title=inp.title)
    return gcal_task_id


async def _find_duplicate_task(inp: CreateTaskInput) -> str | None:
    """Ищет задачу с тем же user_id, title и due_date ±1 день."""
    async with get_session() as session:
        conditions = [
            CalendarTask.user_id == inp.user_id,
            CalendarTask.title == inp.title,
            CalendarTask.status == "needsAction",
        ]
        if inp.due_date is not None:
            conditions += [
                CalendarTask.due_at >= inp.due_date - _IDEMPOTENCY_WINDOW,
                CalendarTask.due_at <= inp.due_date + _IDEMPOTENCY_WINDOW,
            ]
        else:
            conditions.append(CalendarTask.due_at.is_(None))

        result = await session.execute(
            select(CalendarTask.gcal_task_id).where(and_(*conditions)).limit(1)
        )
        return result.scalar_one_or_none()


def _create_in_google(user_id: str, encrypted_token: bytes, inp: CreateTaskInput) -> str:
    """
    Синхронно создаёт задачу в Google Tasks API.
    Вызывается через asyncio.to_thread.
    """
    service = build_tasks_service(encrypted_token)

    body: dict = {"title": inp.title, "status": "needsAction"}
    if inp.notes:
        body["notes"] = inp.notes
    if inp.due_date:
        due_utc = inp.due_date.astimezone(UTC)
        body["due"] = due_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def _call():
        return service.tasks().insert(tasklist="@default", body=body).execute()

    response = call_with_retry(_call, operation="tasks.insert", user_id=user_id)
    return response["id"]


async def _upsert_task(inp: CreateTaskInput, gcal_task_id: str) -> None:
    """INSERT ... ON CONFLICT DO UPDATE."""
    async with get_session() as session:
        stmt = (
            pg_insert(CalendarTask)
            .values(
                user_id=inp.user_id,
                gcal_task_id=gcal_task_id,
                tasklist_id="@default",
                title=inp.title,
                notes=inp.notes,
                due_at=inp.due_date,
                status="needsAction",
                priority=0,
                synced_at=datetime.now(UTC),
            )
            .on_conflict_do_update(
                constraint="uq_calendar_tasks_user_gcal",
                set_={
                    "title": inp.title,
                    "notes": inp.notes,
                    "due_at": inp.due_date,
                    "status": "needsAction",
                    "synced_at": datetime.now(UTC),
                },
            )
        )
        await session.execute(stmt)
        await session.commit()
