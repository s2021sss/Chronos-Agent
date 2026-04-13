"""
get_tasks / get_overdue_tasks — получение задач пользователя.

get_tasks:        локальная БД -> fallback Google Tasks API
get_overdue_tasks: только локальная БД (вызывается в cron-пути)
"""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from chronos_agent.db.engine import get_session
from chronos_agent.db.models import CalendarTask
from chronos_agent.google.client import build_tasks_service
from chronos_agent.logging import get_logger
from chronos_agent.tools._helpers import get_user_token
from chronos_agent.tools.retry import call_with_retry

logger = get_logger(__name__)


async def get_tasks(
    user_id: str,
    status: str = "needsAction",
) -> list[CalendarTask]:
    """
    Возвращает задачи пользователя из локальной БД по статусу.
    При пустом результате — fallback на Google Tasks API с синхронизацией.

    status: "needsAction" | "completed"
    """
    async with get_session() as session:
        result = await session.execute(
            select(CalendarTask)
            .where(
                and_(
                    CalendarTask.user_id == user_id,
                    CalendarTask.status == status,
                )
            )
            .order_by(CalendarTask.priority.desc(), CalendarTask.due_at.asc())
        )
        tasks = list(result.scalars().all())

    if tasks:
        logger.info("get_tasks_from_db", user_id=user_id, status=status, count=len(tasks))
        return tasks

    # Fallback -> Google Tasks API
    logger.info("get_tasks_fallback_google", user_id=user_id, status=status)
    encrypted_token = await get_user_token(user_id)
    fetched = await asyncio.to_thread(_fetch_tasks_from_google, user_id, encrypted_token, status)

    if fetched:
        await _upsert_tasks(user_id, fetched)

    return fetched


async def get_overdue_tasks(user_id: str) -> list[CalendarTask]:
    """
    Возвращает просроченные задачи: status=needsAction И due_at < now().
    Вызывается только в cron-пути (Фаза 9) — только локальная БД, без fallback.
    """
    now = datetime.now(UTC)

    async with get_session() as session:
        result = await session.execute(
            select(CalendarTask)
            .where(
                and_(
                    CalendarTask.user_id == user_id,
                    CalendarTask.status == "needsAction",
                    CalendarTask.due_at < now,
                    CalendarTask.due_at.is_not(None),
                )
            )
            .order_by(CalendarTask.due_at.asc())
        )
        tasks = list(result.scalars().all())

    logger.info("get_overdue_tasks", user_id=user_id, count=len(tasks))
    return tasks


def _fetch_tasks_from_google(
    user_id: str,
    encrypted_token: bytes,
    status: str,
) -> list[CalendarTask]:
    """
    Синхронно запрашивает задачи из Google Tasks API.
    Вызывается через asyncio.to_thread.
    """
    service = build_tasks_service(encrypted_token)

    def _call():
        return (
            service.tasks()
            .list(
                tasklist="@default",
                showCompleted=(status == "completed"),
                showHidden=(status == "completed"),
                maxResults=100,
            )
            .execute()
        )

    response = call_with_retry(_call, operation="tasks.list", user_id=user_id)
    items = response.get("items", [])

    tasks = []
    for item in items:
        # Фильтруем по статусу на клиенте (API возвращает оба если showCompleted=True)
        if item.get("status", "needsAction") != status:
            continue

        due_at = None
        if item.get("due"):
            try:
                due_at = datetime.fromisoformat(item["due"].replace("Z", "+00:00"))
            except ValueError:
                pass

        completed_at = None
        if item.get("completed"):
            try:
                completed_at = datetime.fromisoformat(item["completed"].replace("Z", "+00:00"))
            except ValueError:
                pass

        tasks.append(
            CalendarTask(
                user_id=user_id,
                gcal_task_id=item["id"],
                tasklist_id="@default",
                title=item.get("title", "(без названия)"),
                notes=item.get("notes"),
                due_at=due_at,
                status=item.get("status", "needsAction"),
                completed_at=completed_at,
                priority=0,
                raw_json=item,
            )
        )

    logger.info("get_tasks_google_fetched", user_id=user_id, status=status, count=len(tasks))
    return tasks


async def _upsert_tasks(user_id: str, tasks: list[CalendarTask]) -> None:
    """
    Сохраняет задачи в локальную БД через INSERT ... ON CONFLICT DO UPDATE.
    """
    async with get_session() as session:
        for task in tasks:
            stmt = (
                pg_insert(CalendarTask)
                .values(
                    user_id=task.user_id,
                    gcal_task_id=task.gcal_task_id,
                    tasklist_id=task.tasklist_id,
                    title=task.title,
                    notes=task.notes,
                    due_at=task.due_at,
                    status=task.status,
                    completed_at=task.completed_at,
                    priority=task.priority,
                    raw_json=task.raw_json,
                )
                .on_conflict_do_update(
                    constraint="uq_calendar_tasks_user_gcal",
                    set_={
                        "title": task.title,
                        "notes": task.notes,
                        "due_at": task.due_at,
                        "status": task.status,
                        "completed_at": task.completed_at,
                        "priority": task.priority,
                        "raw_json": task.raw_json,
                        "synced_at": datetime.now(UTC),
                    },
                )
            )
            await session.execute(stmt)
        await session.commit()
