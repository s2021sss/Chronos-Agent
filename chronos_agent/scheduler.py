"""
APScheduler — проактивный flow и обслуживание webhook-каналов.

Jobs:
  cron_check        — каждые cron_interval_minutes минут:
                      проверяем просроченные задачи у всех активных пользователей
  webhook_renewal   — каждые webhook_renewal_interval_days дней:
                      обновляем Google Calendar push-каналы до истечения
"""

import asyncio
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, update

from chronos_agent.config import settings
from chronos_agent.db.engine import get_session
from chronos_agent.db.models import User
from chronos_agent.logging import get_logger

logger = get_logger(__name__)

_scheduler: AsyncIOScheduler | None = None


async def _cron_check_job() -> None:
    """
    Проактивный flow: находит просроченные задачи у всех активных пользователей
    и отправляет им уведомление в Telegram.

    Обрабатывает пользователей последовательно (не параллельно), чтобы не
    перегружать Google API и Telegram rate-limits.
    """
    logger.info("cron_check_started")
    started_at = datetime.now(UTC)

    async with get_session() as session:
        result = await session.execute(select(User.user_id).where(User.status == "active"))
        user_ids = list(result.scalars().all())

    if not user_ids:
        logger.info("cron_check_no_active_users")
        return

    from chronos_agent.tools.get_tasks import get_overdue_tasks
    from chronos_agent.tools.notify_user import NotifyInput, notify_user

    users_notified = 0
    for user_id in user_ids:
        try:
            overdue = await get_overdue_tasks(user_id)
            if not overdue:
                continue

            rows = []
            for t in overdue[:5]:
                due_str = ""
                if t.due_at:
                    due_str = f" (срок: {t.due_at.strftime('%d.%m %H:%M')})"
                rows.append(f"• {t.title}{due_str}")

            titles = "\n".join(rows)
            extra = f"\n…и ещё {len(overdue) - 5}" if len(overdue) > 5 else ""
            text = (
                f"⏰ У тебя {len(overdue)} просроченн"
                f"{'ая задача' if len(overdue) == 1 else 'ых задач'}:\n\n"
                f"{titles}{extra}\n\n"
                "Хочешь отметить выполненными или перенести срок? Просто напиши."
            )

            await notify_user(NotifyInput(user_id=user_id, text=text))
            users_notified += 1

        except Exception as exc:
            logger.warning("cron_check_user_failed", user_id=user_id, error=str(exc))

    duration_ms = int((datetime.now(UTC) - started_at).total_seconds() * 1000)
    logger.info(
        "cron_check_completed",
        total_users=len(user_ids),
        users_notified=users_notified,
        duration_ms=duration_ms,
    )


async def _webhook_renewal_job() -> None:
    """
    Обновляет Google Calendar Events push-каналы, истекающие в ближайшие 24 часа.

    Для каждого пользователя:
      1. Останавливает старый канал (stop())
      2. Регистрирует новый (watch())
      3. Сохраняет новые channel_id / resource_id / expiry в БД

    Non-fatal: ошибки отдельных пользователей логируются и пропускаются.
    Пропускается целиком если GOOGLE_WEBHOOK_BASE_URL не задан.
    """
    logger.info("webhook_renewal_started")

    if not settings.google_webhook_base_url:
        logger.info(
            "webhook_renewal_skipped",
            reason="GOOGLE_WEBHOOK_BASE_URL not configured",
        )
        return

    expiry_threshold = datetime.now(UTC) + timedelta(hours=24)

    async with get_session() as session:
        result = await session.execute(
            select(User).where(
                User.status == "active",
                User.gcal_refresh_token.is_not(None),
                User.gcal_events_channel_expiry.is_not(None),
                User.gcal_events_channel_expiry < expiry_threshold,
            )
        )
        users = list(result.scalars().all())

    if not users:
        logger.info("webhook_renewal_no_expiring_channels")
        return

    from chronos_agent.google.client import build_calendar_service
    from chronos_agent.google.webhooks import (
        register_calendar_events_channel,
        stop_calendar_channel,
    )

    renewed = 0
    for user in users:
        try:
            calendar_service = await asyncio.to_thread(
                build_calendar_service, user.gcal_refresh_token
            )

            if user.gcal_events_channel_id and user.gcal_events_resource_id:
                await asyncio.to_thread(
                    stop_calendar_channel,
                    calendar_service,
                    user.gcal_events_channel_id,
                    user.gcal_events_resource_id,
                )

            channel_info = await asyncio.to_thread(
                register_calendar_events_channel,
                user.user_id,
                calendar_service,
                settings.google_webhook_base_url,
            )

            if channel_info is None:
                logger.warning(
                    "webhook_renewal_channel_failed",
                    user_id=user.user_id,
                    reason="register_calendar_events_channel returned None",
                )
                continue

            async with get_session() as session:
                await session.execute(
                    update(User)
                    .where(User.user_id == user.user_id)
                    .values(
                        gcal_events_channel_id=channel_info.channel_id,
                        gcal_events_resource_id=channel_info.resource_id,
                        gcal_events_channel_expiry=channel_info.expiry,
                    )
                )
                await session.commit()

            renewed += 1
            logger.info("webhook_renewal_channel_renewed", user_id=user.user_id)

        except Exception as exc:
            logger.warning("webhook_renewal_user_failed", user_id=user.user_id, error=str(exc))

    logger.info("webhook_renewal_completed", renewed=renewed, total=len(users))


async def _expire_conversations_job() -> None:
    """Закрывает диалоги с истёкшим таймаутом (каждые 15 минут)."""
    from chronos_agent.memory.session import close_expired_conversations

    try:
        count = await close_expired_conversations(
            timeout_minutes=settings.conversation_timeout_minutes
        )
        if count:
            logger.info("expire_conversations_job_done", closed=count)
    except Exception as exc:
        logger.warning("expire_conversations_job_failed", error=str(exc))


async def _service_heartbeat_job() -> None:
    """
    Обновляет last_alive_at в service_heartbeat каждые heartbeat_interval_seconds.

    Пишет shutdown_type="crash" — если процесс упадёт, запись останется с этим
    значением и recovery при следующем старте корректно определит простой.
    Graceful shutdown перезапишет значение на "graceful" в lifespan.
    """
    from chronos_agent.recovery import write_heartbeat

    try:
        await write_heartbeat(shutdown_type="crash")
    except Exception as exc:
        logger.warning("service_heartbeat_failed", error=str(exc))


def start_scheduler() -> AsyncIOScheduler:
    """
    Создаёт и запускает AsyncIOScheduler с периодическими jobs.
    Вызывается из main.py lifespan (Шаг 8).

    Jobs:
      cron_check          — каждые cron_interval_minutes минут
      webhook_renewal     — каждые webhook_renewal_interval_days дней

    Возвращает запущенный scheduler (для stop_scheduler).
    """
    global _scheduler

    _scheduler = AsyncIOScheduler(
        job_defaults={
            "misfire_grace_time": 60,
            "max_instances": 1,
            "coalesce": True,
        }
    )

    _scheduler.add_job(
        _service_heartbeat_job,
        trigger=IntervalTrigger(seconds=settings.heartbeat_interval_seconds),
        id="service_heartbeat",
        name="Heartbeat: обновление last_alive_at",
        replace_existing=True,
    )

    _scheduler.add_job(
        _cron_check_job,
        trigger=IntervalTrigger(minutes=settings.cron_interval_minutes),
        id="cron_check",
        name="Cron: проверка просроченных задач",
        replace_existing=True,
    )

    _scheduler.add_job(
        _webhook_renewal_job,
        trigger=IntervalTrigger(days=settings.webhook_renewal_interval_days),
        id="webhook_renewal",
        name="Webhook: обновление Google-каналов",
        replace_existing=True,
    )

    _scheduler.add_job(
        _expire_conversations_job,
        trigger=IntervalTrigger(minutes=15),
        id="expire_conversations",
        name="Conversations: закрытие просроченных диалогов",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info(
        "scheduler_started",
        heartbeat_interval_seconds=settings.heartbeat_interval_seconds,
        cron_interval_minutes=settings.cron_interval_minutes,
        webhook_renewal_days=settings.webhook_renewal_interval_days,
    )
    return _scheduler


def stop_scheduler() -> None:
    """
    Останавливает планировщик при shutdown FastAPI.
    Вызывается из main.py lifespan в секции shutdown.
    wait=False — не ждать завершения текущих jobs (graceful enough для PoC).
    """
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("scheduler_stopped")
