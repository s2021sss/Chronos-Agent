"""
Синхронизация событий Google Calendar в локальную БД.
"""

import asyncio
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.dialects.postgresql import insert as pg_insert

from chronos_agent.db.engine import get_session
from chronos_agent.db.models import CalendarEvent
from chronos_agent.google.client import build_calendar_service
from chronos_agent.logging import get_logger
from chronos_agent.tools.retry import call_with_retry

logger = get_logger(__name__)

_SYNC_LOOKBACK_MINUTES = 10
_MAX_RESULTS = 50


async def sync_calendar_events(user_id: str, encrypted_token: bytes) -> int:
    """
    Запрашивает недавно обновлённые события из Google Calendar и upsert-ит их в БД.

    Возвращает количество синхронизированных событий (0 если изменений нет).
    Поднимает OAuthExpiredError если токен истёк.
    Поднимает CalendarAPIError при non-retryable ошибке Google API.
    """
    updated_min = datetime.now(UTC) - timedelta(minutes=_SYNC_LOOKBACK_MINUTES)

    events = await asyncio.to_thread(_fetch_updated_events, user_id, encrypted_token, updated_min)

    if not events:
        return 0

    await _upsert_events(events)

    logger.info(
        "webhook_db_updated",
        user_id=user_id,
        count=len(events),
    )
    return len(events)


def _fetch_updated_events(
    user_id: str,
    encrypted_token: bytes,
    updated_min: datetime,
) -> list[CalendarEvent]:
    """
    Синхронно запрашивает недавно обновлённые события из Google Calendar API.
    Вызывается через asyncio.to_thread.

    showDeleted=True — включаем удалённые (status=cancelled) для полного отражения изменений.
    singleEvents=True — разворачиваем повторяющиеся события в отдельные экземпляры.
    """
    service = build_calendar_service(encrypted_token)
    updated_min_str = updated_min.isoformat().replace("+00:00", "Z")

    def _call():
        return (
            service.events()
            .list(
                calendarId="primary",
                updatedMin=updated_min_str,
                maxResults=_MAX_RESULTS,
                singleEvents=True,
                showDeleted=True,
            )
            .execute()
        )

    response = call_with_retry(_call, operation="events.list_updated", user_id=user_id)
    items = response.get("items", [])

    events: list[CalendarEvent] = []
    for item in items:
        start_raw = item.get("start", {})
        end_raw = item.get("end", {})
        start_str = start_raw.get("dateTime") or start_raw.get("date")
        end_str = end_raw.get("dateTime") or end_raw.get("date")
        if not start_str or not end_str:
            continue

        is_all_day = "date" in start_raw and "dateTime" not in start_raw
        events.append(
            CalendarEvent(
                user_id=user_id,
                gcal_event_id=item["id"],
                calendar_id="primary",
                title=item.get("summary", "(без названия)"),
                description=item.get("description"),
                location=item.get("location"),
                start_at=_parse_dt(start_str, is_all_day),
                end_at=_parse_dt(end_str, is_all_day),
                is_all_day=is_all_day,
                status=item.get("status", "confirmed"),
                recurrence="\n".join(item["recurrence"]) if item.get("recurrence") else None,
                raw_json=item,
            )
        )

    logger.info(
        "sync_calendar_fetched",
        user_id=user_id,
        count=len(events),
        updated_min=updated_min_str,
    )
    return events


def _parse_dt(value: str, is_all_day: bool) -> datetime:
    """Парсит строку datetime/date из Google Calendar API в timezone-aware datetime."""
    if is_all_day:
        d = date.fromisoformat(value)
        return datetime(d.year, d.month, d.day, tzinfo=UTC)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _upsert_events(events: list[CalendarEvent]) -> None:
    """
    Сохраняет события в локальную БД через INSERT ... ON CONFLICT DO UPDATE.
    Идемпотентно — повторные вызовы обновляют существующие записи.
    Constraint: uq_calendar_events_user_gcal (user_id, gcal_event_id).
    """
    async with get_session() as session:
        for event in events:
            stmt = (
                pg_insert(CalendarEvent)
                .values(
                    user_id=event.user_id,
                    gcal_event_id=event.gcal_event_id,
                    calendar_id=event.calendar_id,
                    title=event.title,
                    description=event.description,
                    location=event.location,
                    start_at=event.start_at,
                    end_at=event.end_at,
                    is_all_day=event.is_all_day,
                    status=event.status,
                    recurrence=event.recurrence,
                    raw_json=event.raw_json,
                )
                .on_conflict_do_update(
                    constraint="uq_calendar_events_user_gcal",
                    set_={
                        "title": event.title,
                        "description": event.description,
                        "location": event.location,
                        "start_at": event.start_at,
                        "end_at": event.end_at,
                        "is_all_day": event.is_all_day,
                        "status": event.status,
                        "recurrence": event.recurrence,
                        "raw_json": event.raw_json,
                        "synced_at": datetime.now(UTC),
                    },
                )
            )
            await session.execute(stmt)
        await session.commit()
