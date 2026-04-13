"""
Catch-up синхронизация после простоя сервиса.

Вызывается при старте FastAPI приложения и выполняет следующие шаги:

1. Читает last_alive_at из service_heartbeat.
2. Вычисляет downtime = now() - last_alive_at.
3. Если downtime > recovery_min_downtime_seconds:
     - Для каждого активного пользователя с токеном:
         a. Google Calendar Events: fetch с updatedMin=last_alive_at -> upsert в БД
         b. Google Tasks: полный fetch (Tasks API не поддерживает updatedMin) -> upsert
4. Только после успешного завершения всех синхронизаций:
     обновляет last_alive_at = now() в БД.
   Если хоть один пользователь упал — last_alive_at НЕ обновляется,
   следующий рестарт повторит sync с той же точки.

Правило обновления last_alive_at:
  - В норме: APScheduler heartbeat job каждые heartbeat_interval_seconds
  - При старте: ТОЛЬКО после успешной catch-up sync (см. п.4 выше)
  - На shutdown: write_heartbeat(shutdown_type="graceful")
  Это гарантирует, что при повторных падениях sync-окно не сдвигается
  до тех пор, пока все данные не будут реально загружены.
"""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from chronos_agent.config import settings
from chronos_agent.db.engine import get_session
from chronos_agent.db.models import CalendarTask, ServiceHeartbeat, User
from chronos_agent.logging import get_logger

logger = get_logger(__name__)


async def read_heartbeat() -> datetime | None:
    """
    Возвращает last_alive_at из service_heartbeat или None если записи нет.
    None — первый запуск сервиса, recovery не нужна.
    """
    async with get_session() as session:
        result = await session.execute(select(ServiceHeartbeat).where(ServiceHeartbeat.id == 1))
        row = result.scalar_one_or_none()
    return row.last_alive_at if row else None


async def write_heartbeat(shutdown_type: str = "crash") -> None:
    """
    Upsert last_alive_at = now() в service_heartbeat (id=1).

    Вызывается:
      - APScheduler heartbeat job (shutdown_type="crash" — на случай падения)
      - После успешной catch-up sync при старте (shutdown_type="crash")
      - На graceful shutdown (shutdown_type="graceful")
    """
    now = datetime.now(UTC)
    async with get_session() as session:
        stmt = (
            pg_insert(ServiceHeartbeat)
            .values(
                id=1,
                last_alive_at=now,
                shutdown_type=shutdown_type,
            )
            .on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "last_alive_at": now,
                    "shutdown_type": shutdown_type,
                },
            )
        )
        await session.execute(stmt)
        await session.commit()


async def _sync_events_since(user_id: str, encrypted_token: bytes, since: datetime) -> int:
    """
    Запрашивает события Google Calendar обновлённые после `since` и upsert-ит в БД.
    Возвращает кол-во синхронизированных событий.

    Переиспользует логику из google/sync.py, но с кастомным updated_min
    вместо фиксированных 10 минут.
    """
    from chronos_agent.google.sync import _fetch_updated_events, _upsert_events

    events = await asyncio.to_thread(_fetch_updated_events, user_id, encrypted_token, since)
    if events:
        await _upsert_events(events)
    return len(events)


async def _sync_tasks_full(user_id: str, encrypted_token: bytes) -> int:
    """
    Полная синхронизация задач из Google Tasks API -> БД.

    Google Tasks не поддерживает updatedMin, поэтому делаем полный fetch
    как для needsAction так и для completed и upsert-им всё.
    Upsert идемпотентен — дубликатов не будет.
    """
    from chronos_agent.google.client import build_tasks_service
    from chronos_agent.tools.retry import call_with_retry

    def _fetch_tasks(status: str) -> list[dict]:
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

        response = call_with_retry(_call, operation="recovery_tasks.list", user_id=user_id)
        return response.get("items", [])

    items_active = await asyncio.to_thread(_fetch_tasks, "needsAction")
    items_done = await asyncio.to_thread(_fetch_tasks, "completed")
    all_items = items_active + items_done

    now = datetime.now(UTC)
    tasks: list[CalendarTask] = []
    for item in all_items:
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

    if tasks:
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
                            "synced_at": now,
                        },
                    )
                )
                await session.execute(stmt)
            await session.commit()

    logger.info("recovery_tasks_synced", user_id=user_id, count=len(tasks))
    return len(tasks)


async def run_startup_recovery() -> None:
    """
    Точка входа — вызывается из main.py lifespan после AgentCore.init.

    Алгоритм:
      1. Читаем last_alive_at. Если нет — первый запуск, выходим.
      2. Считаем downtime. Если меньше порога — выходим (webhook успел доставить).
      3. Собираем активных пользователей с Google токеном.
      4. Для каждого: sync events (updatedMin=last_alive_at) + sync tasks (full).
      5. Если все успешно — write_heartbeat() для сдвига точки отсчёта.
         Если хоть один упал — оставляем last_alive_at как есть.
    """
    last_alive_at = await read_heartbeat()

    if last_alive_at is None:
        logger.info("recovery_skipped", reason="first_start_no_heartbeat")
        return

    now = datetime.now(UTC)
    downtime_seconds = (now - last_alive_at).total_seconds()

    if downtime_seconds < settings.recovery_min_downtime_seconds:
        logger.info(
            "recovery_skipped",
            reason="downtime_below_threshold",
            downtime_seconds=round(downtime_seconds),
            threshold=settings.recovery_min_downtime_seconds,
        )
        await write_heartbeat()
        return

    logger.info(
        "recovery_started",
        last_alive_at=last_alive_at.isoformat(),
        downtime_seconds=round(downtime_seconds),
    )

    async with get_session() as session:
        result = await session.execute(
            select(User).where(
                User.status == "active",
                User.gcal_refresh_token.is_not(None),
            )
        )
        users = list(result.scalars().all())

    if not users:
        logger.info("recovery_no_users")
        await write_heartbeat()
        return

    failed_users: list[str] = []
    total_events = 0
    total_tasks = 0

    for user in users:
        user_failed = False
        try:
            n_events = await _sync_events_since(
                user.user_id, user.gcal_refresh_token, last_alive_at
            )
            total_events += n_events
        except Exception as exc:
            logger.warning(
                "recovery_events_failed",
                user_id=user.user_id,
                error=str(exc),
            )
            user_failed = True

        try:
            n_tasks = await _sync_tasks_full(user.user_id, user.gcal_refresh_token)
            total_tasks += n_tasks
        except Exception as exc:
            logger.warning(
                "recovery_tasks_failed",
                user_id=user.user_id,
                error=str(exc),
            )
            user_failed = True

        if user_failed:
            failed_users.append(user.user_id)

    if failed_users:
        logger.warning(
            "recovery_partial_failure",
            failed_users=failed_users,
            succeeded=len(users) - len(failed_users),
            total_events=total_events,
            total_tasks=total_tasks,
            note="last_alive_at NOT updated — will retry on next restart",
        )
    else:
        await write_heartbeat()
        logger.info(
            "recovery_completed",
            users_synced=len(users),
            total_events=total_events,
            total_tasks=total_tasks,
            downtime_seconds=round(downtime_seconds),
        )
