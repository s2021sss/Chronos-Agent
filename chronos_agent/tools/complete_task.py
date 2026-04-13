"""
complete_task — отметить задачу выполненной.

Side effects:
  - Обновление статуса в Google Tasks API (status=completed)
  - Обновление локальной БД (status=completed, completed_at=now)
"""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select, update

from chronos_agent.db.engine import get_session
from chronos_agent.db.models import CalendarTask
from chronos_agent.google.client import build_tasks_service
from chronos_agent.logging import get_logger
from chronos_agent.tools._helpers import get_user_token
from chronos_agent.tools.exceptions import TaskNotFoundError
from chronos_agent.tools.retry import call_with_retry

logger = get_logger(__name__)


async def complete_task(user_id: str, task_id: str) -> None:
    """
    Отмечает задачу выполненной в Google Tasks и локальной БД.

    task_id: gcal_task_id из локальной БД.
    Поднимает TaskNotFoundError если задача не найдена.
    """
    # Проверяем существование в локальной БД
    async with get_session() as session:
        result = await session.execute(
            select(CalendarTask).where(
                CalendarTask.user_id == user_id,
                CalendarTask.gcal_task_id == task_id,
            )
        )
        task = result.scalar_one_or_none()

    if task is None:
        raise TaskNotFoundError(f"Task '{task_id}' not found for user {user_id}")

    if task.status == "completed":
        logger.info("complete_task_already_done", user_id=user_id, gcal_task_id=task_id)
        return

    # Обновление в Google Tasks API
    encrypted_token = await get_user_token(user_id)
    await asyncio.to_thread(_complete_in_google, user_id, encrypted_token, task_id)

    # Обновление локальной БД
    now = datetime.now(UTC)
    async with get_session() as session:
        await session.execute(
            update(CalendarTask)
            .where(
                CalendarTask.user_id == user_id,
                CalendarTask.gcal_task_id == task_id,
            )
            .values(
                status="completed",
                completed_at=now,
                synced_at=now,
            )
        )
        await session.commit()

    logger.info("complete_task_done", user_id=user_id, gcal_task_id=task_id, title=task.title)


def _complete_in_google(user_id: str, encrypted_token: bytes, task_id: str) -> None:
    """
    Синхронно обновляет статус задачи в Google Tasks API (PATCH).
    Вызывается через asyncio.to_thread.
    """
    service = build_tasks_service(encrypted_token)

    def _call():
        return (
            service.tasks()
            .patch(
                tasklist="@default",
                task=task_id,
                body={"status": "completed"},
            )
            .execute()
        )

    call_with_retry(_call, operation="tasks.patch", user_id=user_id)
