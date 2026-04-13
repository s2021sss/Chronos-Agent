"""
create_event — создание события в Google Calendar + локальная БД.

Идемпотентность:
  Перед вызовом API проверяет наличие события с тем же user_id, title, start ±1 мин.
  При совпадении возвращает существующий gcal_event_id без повторного создания.

Санация:
  title и description очищаются через sanitize.py (Pydantic validator).

Sanity checks:
  - end > start
  - start не в прошлом более чем на 1 час
  - start не в будущем более чем на 2 года
  - title не пустой, ≤ 256 символов
"""

import asyncio
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, field_validator, model_validator
from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from chronos_agent.db.engine import get_session
from chronos_agent.db.models import CalendarEvent
from chronos_agent.google.client import build_calendar_service
from chronos_agent.logging import get_logger
from chronos_agent.tools._helpers import get_user_token
from chronos_agent.tools.exceptions import ToolValidationError
from chronos_agent.tools.retry import call_with_retry
from chronos_agent.tools.sanitize import sanitize_text, sanitize_title

logger = get_logger(__name__)

_IDEMPOTENCY_WINDOW = timedelta(minutes=1)
_MAX_PAST_HOURS = 1
_MAX_FUTURE_YEARS = 2


class CreateEventInput(BaseModel):
    user_id: str
    title: str
    start: datetime
    end: datetime
    description: str | None = None

    @field_validator("title")
    @classmethod
    def _sanitize_title(cls, v: str) -> str:
        return sanitize_title(v)

    @field_validator("description")
    @classmethod
    def _sanitize_description(cls, v: str | None) -> str | None:
        return sanitize_text(v)

    @model_validator(mode="after")
    def _validate_times(self) -> "CreateEventInput":
        now = datetime.now(UTC)

        start = self.start if self.start.tzinfo else self.start.replace(tzinfo=UTC)
        end = self.end if self.end.tzinfo else self.end.replace(tzinfo=UTC)

        if end <= start:
            raise ToolValidationError("end must be after start")

        if start < now - timedelta(hours=_MAX_PAST_HOURS):
            raise ToolValidationError(
                f"start is more than {_MAX_PAST_HOURS}h in the past: {start.isoformat()}"
            )

        max_future = now.replace(year=now.year + _MAX_FUTURE_YEARS)
        if start > max_future:
            raise ToolValidationError(
                f"start is more than {_MAX_FUTURE_YEARS} years in the future: {start.isoformat()}"
            )

        self.start = start
        self.end = end
        return self

    model_config = {"arbitrary_types_allowed": True}


async def create_event(inp: CreateEventInput) -> str:
    """
    Создаёт событие в Google Calendar и локальной БД.
    Возвращает gcal_event_id.

    Идемпотентен: повторный вызов с теми же параметрами вернёт существующий ID.
    """
    # Idempotency check
    existing_id = await _find_duplicate_event(inp)
    if existing_id:
        logger.info("create_event_duplicate", user_id=inp.user_id, gcal_event_id=existing_id)
        return existing_id

    # Создание в Google Calendar
    encrypted_token = await get_user_token(inp.user_id)
    gcal_event_id = await asyncio.to_thread(_create_in_google, inp.user_id, encrypted_token, inp)

    # Сохранение в локальную БД
    await _upsert_event(inp, gcal_event_id)

    logger.info(
        "create_event_done",
        user_id=inp.user_id,
        gcal_event_id=gcal_event_id,
        title=inp.title,
    )
    return gcal_event_id


async def _find_duplicate_event(inp: CreateEventInput) -> str | None:
    """
    Ищет событие с тем же user_id, title и start ±1 мин в локальной БД.
    """
    async with get_session() as session:
        result = await session.execute(
            select(CalendarEvent.gcal_event_id)
            .where(
                and_(
                    CalendarEvent.user_id == inp.user_id,
                    CalendarEvent.title == inp.title,
                    CalendarEvent.start_at >= inp.start - _IDEMPOTENCY_WINDOW,
                    CalendarEvent.start_at <= inp.start + _IDEMPOTENCY_WINDOW,
                    CalendarEvent.status != "cancelled",
                )
            )
            .limit(1)
        )
        row = result.scalar_one_or_none()

    return row


def _create_in_google(user_id: str, encrypted_token: bytes, inp: CreateEventInput) -> str:
    """
    Синхронно создаёт событие в Google Calendar API.
    Вызывается через asyncio.to_thread.
    """
    service = build_calendar_service(encrypted_token)

    body: dict = {
        "summary": inp.title,
        "start": {"dateTime": inp.start.isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": inp.end.isoformat(), "timeZone": "UTC"},
    }
    if inp.description:
        body["description"] = inp.description

    def _call():
        return service.events().insert(calendarId="primary", body=body).execute()

    response = call_with_retry(_call, operation="events.insert", user_id=user_id)
    return response["id"]


async def _upsert_event(inp: CreateEventInput, gcal_event_id: str) -> None:
    """INSERT ... ON CONFLICT DO UPDATE — идемпотентная запись в БД."""
    async with get_session() as session:
        stmt = (
            pg_insert(CalendarEvent)
            .values(
                user_id=inp.user_id,
                gcal_event_id=gcal_event_id,
                calendar_id="primary",
                title=inp.title,
                description=inp.description,
                start_at=inp.start,
                end_at=inp.end,
                is_all_day=False,
                status="confirmed",
                synced_at=datetime.now(UTC),
            )
            .on_conflict_do_update(
                constraint="uq_calendar_events_user_gcal",
                set_={
                    "title": inp.title,
                    "description": inp.description,
                    "start_at": inp.start,
                    "end_at": inp.end,
                    "status": "confirmed",
                    "synced_at": datetime.now(UTC),
                },
            )
        )
        await session.execute(stmt)
        await session.commit()
