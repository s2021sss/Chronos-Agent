"""
get_events — получение событий календаря.

Источник данных:
  1. Локальная БД (calendar_events) — основной путь
  2. Google Calendar Events API — fallback при пустом результате из БД

Fallback синхронизирует данные в БД для последующих запросов.
Максимум 50 событий за вызов.
"""

import asyncio
from datetime import UTC, datetime

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from chronos_agent.db.engine import get_session
from chronos_agent.db.models import CalendarEvent
from chronos_agent.google.client import build_calendar_service
from chronos_agent.logging import get_logger
from chronos_agent.tools._helpers import get_user_token
from chronos_agent.tools.exceptions import ToolValidationError
from chronos_agent.tools.retry import call_with_retry

logger = get_logger(__name__)

_MAX_RESULTS = 50


async def get_events(
    user_id: str,
    start: datetime,
    end: datetime,
) -> list[CalendarEvent]:
    """
    Возвращает события пользователя в диапазоне [start, end].

    При пустом результате из БД — fallback на Google Calendar API с синхронизацией.
    Поднимает ToolValidationError если start >= end.
    Поднимает OAuthExpiredError если токен отсутствует или истёк.
    """
    if start >= end:
        raise ToolValidationError(f"start must be before end: {start} >= {end}")

    # ── Шаг 1: Локальная БД ──────────────────────────────────────────────────
    async with get_session() as session:
        result = await session.execute(
            select(CalendarEvent)
            .where(
                and_(
                    CalendarEvent.user_id == user_id,
                    CalendarEvent.start_at < end,
                    CalendarEvent.end_at > start,
                    CalendarEvent.status != "cancelled",
                )
            )
            .order_by(CalendarEvent.start_at)
            .limit(_MAX_RESULTS)
        )
        events = list(result.scalars().all())

    if events:
        logger.info("get_events_from_db", user_id=user_id, count=len(events))
        return events

    # ── Шаг 2: Fallback -> Google Calendar API ────────────────────────────────
    logger.info(
        "get_events_fallback_google",
        user_id=user_id,
        start=start.isoformat(),
        end=end.isoformat(),
    )

    encrypted_token = await get_user_token(user_id)
    fetched = await asyncio.to_thread(
        _fetch_events_from_google, user_id, encrypted_token, start, end
    )

    if fetched:
        await _upsert_events(user_id, fetched)

    return fetched


def _fetch_events_from_google(
    user_id: str,
    encrypted_token: bytes,
    start: datetime,
    end: datetime,
) -> list[CalendarEvent]:
    """
    Синхронно запрашивает события из Google Calendar API.
    Вызывается через asyncio.to_thread.
    """
    service = build_calendar_service(encrypted_token)

    def _call():
        return (
            service.events()
            .list(
                calendarId="primary",
                timeMin=start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                timeMax=end.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                maxResults=_MAX_RESULTS,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )

    response = call_with_retry(_call, operation="events.list", user_id=user_id)
    items = response.get("items", [])

    events = []
    for item in items:
        start_raw = item.get("start", {})
        end_raw = item.get("end", {})

        start_str = start_raw.get("dateTime") or start_raw.get("date")
        end_str = end_raw.get("dateTime") or end_raw.get("date")
        if not start_str or not end_str:
            continue

        is_all_day = "date" in start_raw and "dateTime" not in start_raw
        event_start = _parse_google_dt(start_str, is_all_day)
        event_end = _parse_google_dt(end_str, is_all_day)

        events.append(
            CalendarEvent(
                user_id=user_id,
                gcal_event_id=item["id"],
                calendar_id="primary",
                title=item.get("summary", "(без названия)"),
                description=item.get("description"),
                location=item.get("location"),
                start_at=event_start,
                end_at=event_end,
                is_all_day=is_all_day,
                status=item.get("status", "confirmed"),
                recurrence="\n".join(item["recurrence"]) if item.get("recurrence") else None,
                raw_json=item,
            )
        )

    logger.info("get_events_google_fetched", user_id=user_id, count=len(events))
    return events


def _parse_google_dt(value: str, is_all_day: bool) -> datetime:
    """Парсит datetime строку из Google Calendar API в timezone-aware datetime."""
    if is_all_day:
        from datetime import date

        d = date.fromisoformat(value)
        return datetime(d.year, d.month, d.day, tzinfo=UTC)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


async def _upsert_events(user_id: str, events: list[CalendarEvent]) -> None:
    """
    Сохраняет события в локальную БД через INSERT ... ON CONFLICT DO UPDATE.
    Идемпотентно — повторные вызовы обновляют существующие записи.
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
