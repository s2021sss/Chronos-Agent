"""
move_event — перенос события на другой временной слот.

Sanity checks:
  - new_end > new_start
  - new_start не в прошлом более чем на 1 час
  - Событие должно существовать в локальной БД
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, model_validator
from sqlalchemy import select, update

from chronos_agent.db.engine import get_session
from chronos_agent.db.models import CalendarEvent
from chronos_agent.google.client import build_calendar_service
from chronos_agent.logging import get_logger
from chronos_agent.tools._helpers import get_user_token
from chronos_agent.tools.exceptions import TaskNotFoundError, ToolValidationError
from chronos_agent.tools.retry import call_with_retry

logger = get_logger(__name__)

_MAX_PAST_HOURS = 1
_HITL_THRESHOLD_HOURS = 2


@dataclass
class MoveEventResult:
    gcal_event_id: str
    requires_confirmation: bool


class MoveEventInput(BaseModel):
    user_id: str
    event_id: str
    new_start: datetime
    new_end: datetime

    @model_validator(mode="after")
    def _validate_times(self) -> "MoveEventInput":
        now = datetime.now(UTC)

        new_start = self.new_start if self.new_start.tzinfo else self.new_start.replace(tzinfo=UTC)
        new_end = self.new_end if self.new_end.tzinfo else self.new_end.replace(tzinfo=UTC)

        if new_end <= new_start:
            raise ToolValidationError("new_end must be after new_start")

        if new_start < now - timedelta(hours=_MAX_PAST_HOURS):
            raise ToolValidationError(
                f"new_start is more than {_MAX_PAST_HOURS}h in the past: {new_start.isoformat()}"
            )

        self.new_start = new_start
        self.new_end = new_end
        return self

    model_config = {"arbitrary_types_allowed": True}


async def move_event(inp: MoveEventInput) -> MoveEventResult:
    """
    Переносит событие на новый слот в Google Calendar и обновляет локальную БД.

    Возвращает MoveEventResult с флагом requires_confirmation:
      True  -> длительность > 2 ч; оркестратор должен был запросить HITL до вызова
      False -> перенос выполнен без подтверждения
    """
    # Проверяем существование события в локальной БД
    async with get_session() as session:
        result = await session.execute(
            select(CalendarEvent).where(
                CalendarEvent.user_id == inp.user_id,
                CalendarEvent.gcal_event_id == inp.event_id,
                CalendarEvent.status != "cancelled",
            )
        )
        event = result.scalar_one_or_none()

    if event is None:
        raise TaskNotFoundError(f"Event '{inp.event_id}' not found for user {inp.user_id}")

    # Guardrail: длительность > 2 ч
    duration_hours = (inp.new_end - inp.new_start).total_seconds() / 3600
    requires_confirmation = duration_hours > _HITL_THRESHOLD_HOURS

    # Обновление в Google Calendar
    encrypted_token = await get_user_token(inp.user_id)
    await asyncio.to_thread(_update_in_google, inp.user_id, encrypted_token, inp)

    # Обновление локальной БД
    async with get_session() as session:
        await session.execute(
            update(CalendarEvent)
            .where(
                CalendarEvent.user_id == inp.user_id,
                CalendarEvent.gcal_event_id == inp.event_id,
            )
            .values(
                start_at=inp.new_start,
                end_at=inp.new_end,
                synced_at=datetime.now(UTC),
            )
        )
        await session.commit()

    logger.info(
        "move_event_done",
        user_id=inp.user_id,
        gcal_event_id=inp.event_id,
        new_start=inp.new_start.isoformat(),
        requires_confirmation=requires_confirmation,
    )
    return MoveEventResult(gcal_event_id=inp.event_id, requires_confirmation=requires_confirmation)


def _update_in_google(user_id: str, encrypted_token: bytes, inp: MoveEventInput) -> None:
    """
    Синхронно обновляет время события в Google Calendar API (PATCH).
    Вызывается через asyncio.to_thread.
    """
    service = build_calendar_service(encrypted_token)

    body = {
        "start": {"dateTime": inp.new_start.isoformat(), "timeZone": "UTC"},
        "end": {"dateTime": inp.new_end.isoformat(), "timeZone": "UTC"},
    }

    def _call():
        return (
            service.events()
            .patch(
                calendarId="primary",
                eventId=inp.event_id,
                body=body,
            )
            .execute()
        )

    call_with_retry(_call, operation="events.patch", user_id=user_id)
