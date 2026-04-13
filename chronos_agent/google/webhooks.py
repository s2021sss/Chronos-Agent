"""
Управление push-каналами Google Calendar.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from googleapiclient.errors import HttpError

from chronos_agent.config import settings
from chronos_agent.logging import get_logger

logger = get_logger(__name__)

_CHANNEL_TTL_SECONDS = settings.webhook_renewal_interval_days * 24 * 3600


@dataclass
class ChannelInfo:
    channel_id: str
    resource_id: str
    expiry: datetime


def register_calendar_events_channel(
    user_id: str,
    calendar_service,
    webhook_base_url: str,
) -> ChannelInfo | None:
    """
    Регистрирует push-канал для событий Google Calendar (Events).
    Синхронный — вызывать через asyncio.to_thread().

    Возвращает ChannelInfo при успехе.
    Возвращает None если:
    - webhook_base_url не настроен (dev без туннеля)
    - регистрация не удалась (non-fatal — бот работает без push, синхронизируется через polling)
    """
    if not webhook_base_url:
        logger.warning(
            "gcal_events_channel_skipped",
            user_id=user_id,
            reason="GOOGLE_WEBHOOK_BASE_URL not configured — sync will use polling only",
        )
        return None

    channel_id = str(uuid.uuid4())
    expiry_dt = datetime.now(UTC) + timedelta(seconds=_CHANNEL_TTL_SECONDS)
    expiry_ms = int(expiry_dt.timestamp() * 1000)

    body = {
        "id": channel_id,
        "type": "web_hook",
        "address": f"{webhook_base_url.rstrip('/')}/webhooks/google/calendar",
        "expiration": str(expiry_ms),
    }

    try:
        response = calendar_service.events().watch(calendarId="primary", body=body).execute()
        resource_id = response["resourceId"]
        actual_expiry = datetime.fromtimestamp(int(response["expiration"]) / 1000, tz=UTC)

        logger.info(
            "gcal_events_channel_registered",
            user_id=user_id,
            channel_id=channel_id,
            resource_id=resource_id,
            expiry=actual_expiry.isoformat(),
        )
        return ChannelInfo(channel_id=channel_id, resource_id=resource_id, expiry=actual_expiry)

    except HttpError as exc:
        logger.warning(
            "gcal_events_channel_failed",
            user_id=user_id,
            status=exc.status_code,
            error=str(exc),
        )
        return None


def stop_calendar_channel(
    calendar_service,
    channel_id: str,
    resource_id: str,
) -> None:
    """
    Останавливает существующий push-канал.
    Синхронный — вызывать через asyncio.to_thread().

    Вызывается перед повторной регистрацией (обновление канала или повторная авторизация).
    Non-fatal — канал мог уже истечь.
    """
    try:
        calendar_service.channels().stop(
            body={"id": channel_id, "resourceId": resource_id}
        ).execute()
        logger.info("gcal_events_channel_stopped", channel_id=channel_id)
    except HttpError as exc:
        logger.warning(
            "gcal_events_channel_stop_failed",
            channel_id=channel_id,
            error=str(exc),
        )
