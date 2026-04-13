"""
find_free_slots — поиск свободных временных слотов.

Алгоритм:
  1. Получить события из БД (get_events) за окно поиска
  2. Для каждого дня в окне найти промежутки >= duration_minutes
     в рабочих часах 08:00–22:00 (по timezone пользователя)
  3. Отсортировать по score, вернуть топ-3
  4. Если в первые search_window_hours нет слотов — расширить до 48h

Скоринг:
  - Близость к preferred_start (вес 0.5)
  - Время суток: утро > день > вечер (вес 0.3)
  - Запас времени >= 30 мин сверх запрошенного (вес 0.2)
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from chronos_agent.db.engine import get_session
from chronos_agent.db.models import User
from chronos_agent.logging import get_logger
from chronos_agent.tools.exceptions import OAuthExpiredError, ToolValidationError
from chronos_agent.tools.get_events import get_events

logger = get_logger(__name__)

_WORK_HOUR_START = 8  # 08:00 по timezone пользователя
_WORK_HOUR_END = 22  # 22:00 по timezone пользователя
_MAX_SLOTS = 3
_FALLBACK_WINDOW_HOURS = 48
_SLOT_ROUND_MINUTES = 15


@dataclass
class CalendarSlot:
    start: datetime
    end: datetime
    duration_minutes: int
    score: float


async def find_free_slots(
    user_id: str,
    duration_minutes: int,
    preferred_start: datetime | None = None,
    search_window_hours: int | None = None,
) -> list[CalendarSlot]:
    """
    Возвращает до 3 свободных слотов >= duration_minutes, отсортированных по score.

    duration_minutes: требуемая длительность в минутах
    preferred_start:  предпочтительное начало (влияет на скоринг)
    search_window_hours: окно поиска в часах (из config по умолчанию)

    Поднимает ToolValidationError если duration_minutes < 1.
    Поднимает OAuthExpiredError если нет токена.
    """
    from chronos_agent.config import settings

    if duration_minutes < 1:
        raise ToolValidationError("duration_minutes must be >= 1")

    now = datetime.now(UTC)
    window_hours = search_window_hours or settings.search_window_hours
    if preferred_start is not None:
        search_start = max(now, preferred_start - timedelta(hours=4))
    else:
        search_start = now

    async with get_session() as session:
        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()

    if user is None:
        raise OAuthExpiredError(f"User {user_id} not found")

    user_tz = ZoneInfo(user.timezone)

    for hours in (window_hours, _FALLBACK_WINDOW_HOURS):
        window_end = search_start + timedelta(hours=hours)
        events = await get_events(user_id, search_start, window_end)
        slots = _find_slots(
            events,
            search_start,
            window_end,
            duration_minutes,
            user_tz,
            preferred_start,
        )

        if slots or hours == _FALLBACK_WINDOW_HOURS:
            logger.info(
                "find_free_slots_result",
                user_id=user_id,
                duration_minutes=duration_minutes,
                search_start=search_start.isoformat(),
                window_hours=hours,
                slots_found=len(slots),
            )
            return slots[:_MAX_SLOTS]

    return []


def _find_slots(
    events: list,
    window_start: datetime,
    window_end: datetime,
    duration_minutes: int,
    user_tz: ZoneInfo,
    preferred_start: datetime | None,
) -> list[CalendarSlot]:
    """
    Находит свободные промежутки в рабочих часах, не перекрытые событиями.
    Возвращает слоты, отсортированные по score убыванию.
    """
    busy: list[tuple[datetime, datetime]] = sorted(
        [(e.start_at, e.end_at) for e in events],
        key=lambda x: x[0],
    )

    slots: list[CalendarSlot] = []

    current_local = window_start.astimezone(user_tz)
    end_local = window_end.astimezone(user_tz)
    current_day = current_local.date()
    last_day = end_local.date()

    while current_day <= last_day:
        work_start = datetime(
            current_day.year,
            current_day.month,
            current_day.day,
            _WORK_HOUR_START,
            0,
            tzinfo=user_tz,
        ).astimezone(UTC)
        work_end = datetime(
            current_day.year,
            current_day.month,
            current_day.day,
            _WORK_HOUR_END,
            0,
            tzinfo=user_tz,
        ).astimezone(UTC)

        day_start = max(work_start, window_start)
        day_end = min(work_end, window_end)

        if day_start < day_end:
            day_slots = _gaps_in_day(
                busy,
                day_start,
                day_end,
                duration_minutes,
                user_tz,
                preferred_start,
            )
            slots.extend(day_slots)

        current_day = current_day + timedelta(days=1)

    slots.sort(key=lambda s: s.score, reverse=True)
    return slots


def _gaps_in_day(
    busy: list[tuple[datetime, datetime]],
    day_start: datetime,
    day_end: datetime,
    duration_minutes: int,
    user_tz: ZoneInfo,
    preferred_start: datetime | None,
) -> list[CalendarSlot]:
    """Находит свободные промежутки в рамках одного рабочего дня."""
    gaps = []
    cursor = day_start

    for event_start, event_end in busy:
        if event_end <= cursor or event_start >= day_end:
            continue

        if event_start > cursor:
            gap_end = min(event_start, day_end)
            _maybe_add_gap(gaps, cursor, gap_end, duration_minutes, user_tz, preferred_start)

        cursor = max(cursor, event_end)

    if cursor < day_end:
        _maybe_add_gap(gaps, cursor, day_end, duration_minutes, user_tz, preferred_start)

    return gaps


def _round_up(dt: datetime, minutes: int) -> datetime:
    """
    Округляет datetime вверх до ближайшего кратного minutes.

    Пример: 16:56 -> 17:00 (при minutes=15).
    Результат всегда >= входного dt, поэтому пересечений с занятыми
    интервалами не возникает — слот остаётся строго внутри свободного промежутка.
    """
    remainder = dt.minute % minutes
    if remainder == 0 and dt.second == 0 and dt.microsecond == 0:
        return dt
    extra = minutes - remainder if remainder != 0 else 0
    rounded = (dt + timedelta(minutes=extra)).replace(second=0, microsecond=0)
    return rounded


def _maybe_add_gap(
    gaps: list[CalendarSlot],
    start: datetime,
    end: datetime,
    duration_minutes: int,
    user_tz: ZoneInfo,
    preferred_start: datetime | None,
) -> None:
    """Добавляет слот если промежуток достаточно длинный.

    Начало слота округляется вверх до ближайших 15 минут (красивое время).
    Проверка на вместимость делается ПОСЛЕ округления — это гарантирует,
    что слот всегда находится внутри свободного промежутка без пересечений.
    """
    rounded_start = _round_up(start, _SLOT_ROUND_MINUTES)
    if rounded_start >= end:
        return
    gap_minutes = int((end - rounded_start).total_seconds() / 60)
    if gap_minutes >= duration_minutes:
        score = _score_slot(rounded_start, gap_minutes, duration_minutes, user_tz, preferred_start)
        gaps.append(
            CalendarSlot(
                start=rounded_start,
                end=end,
                duration_minutes=gap_minutes,
                score=score,
            )
        )


def _score_slot(
    slot_start: datetime,
    slot_minutes: int,
    requested_minutes: int,
    user_tz: ZoneInfo,
    preferred_start: datetime | None,
) -> float:
    """
    Вычисляет score слота по трём компонентам:
      - Близость к preferred_start (вес 0.5)
      - Время суток: 8-12 = 1.0, 12-18 = 0.7, 18-22 = 0.3 (вес 0.3)
      - Запас >= 30 мин сверх запрошенного (вес 0.2)
    """
    score = 0.0

    # Компонента 1: Близость к preferred_start
    if preferred_start is not None:
        diff_hours = abs((slot_start - preferred_start).total_seconds()) / 3600
        proximity = max(0.0, 1.0 - diff_hours / 24.0)
        score += 0.5 * proximity
    else:
        score += 0.25  # нейтральный балл если время не задано

    # Компонента 2: Время суток
    local_hour = slot_start.astimezone(user_tz).hour
    if 8 <= local_hour < 12:
        tod = 1.0
    elif 12 <= local_hour < 18:
        tod = 0.7
    else:
        tod = 0.3
    score += 0.3 * tod

    # Компонента 3: Запас времени
    buffer_min = slot_minutes - requested_minutes
    buffer_score = min(1.0, buffer_min / 30.0)
    score += 0.2 * buffer_score

    return round(score, 4)
