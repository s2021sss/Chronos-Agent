import asyncio
import sys
from contextlib import asynccontextmanager

import sqlalchemy as sa
from aiogram.types import Update
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import Mount
from sqlalchemy import select, update

from chronos_agent.bot.setup import (
    close_bot,
    get_bot,
    get_dp,
    get_webhook_secret,
    init_bot,
    register_bot_commands,
    register_webhook,
)
from chronos_agent.config import settings
from chronos_agent.db.engine import close_db, get_session, init_db
from chronos_agent.db.models import User
from chronos_agent.google.auth import (
    encrypt_refresh_token,
    exchange_code,
    verify_oauth_state,
)
from chronos_agent.google.webhooks import register_calendar_events_channel
from chronos_agent.health import run_all_checks
from chronos_agent.logging import configure_logging, get_logger
from chronos_agent.metrics import metrics_app
from chronos_agent.templates import OAUTH_ERROR_HTML, OAUTH_SUCCESS_HTML

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup / shutdown lifecycle.
    Порядок инициализации строго фиксирован — каждый шаг зависит от предыдущего.
    """
    await init_db()

    try:
        async with get_session() as session:
            await session.execute(sa.text("SELECT 1"))
        logger.info("db_ready")
    except Exception as exc:
        logger.critical("db_unavailable_at_startup", error=str(exc))
        sys.exit(1)

    init_bot()

    try:
        await register_bot_commands()
    except Exception as exc:
        logger.warning("bot_commands_registration_failed", error=str(exc))

    webhook_url = settings.telegram_webhook_url
    if webhook_url:
        try:
            await register_webhook(webhook_url)
        except Exception as exc:
            logger.warning(
                "webhook_registration_failed",
                error=str(exc),
                hint="Telegram may be blocked in container network. "
                "Webhook stays registered from previous run.",
            )
    else:
        logger.warning(
            "webhook_not_registered",
            reason="TELEGRAM_WEBHOOK_URL not set in .env",
        )

    # Whisper
    from chronos_agent.whisper import WhisperSingleton

    await asyncio.to_thread(
        WhisperSingleton.load,
        settings.whisper_model,
        settings.whisper_device,
        settings.whisper_compute_type,
    )

    # AgentCore
    from chronos_agent.agent.core import AgentCore

    pg_conn_string = settings.postgres_url.replace("postgresql+asyncpg://", "postgresql://")
    await AgentCore.init(pg_conn_string)

    await AgentCore.recover_orphaned_sessions()

    # Синхронизация событий и задач за время простоя сервиса
    from chronos_agent.recovery import run_startup_recovery

    await run_startup_recovery()

    # APScheduler
    from chronos_agent.scheduler import start_scheduler

    start_scheduler()

    logger.info("service_started", host="0.0.0.0", port=8000)

    yield

    logger.info("service_stopping")

    from chronos_agent.recovery import write_heartbeat
    from chronos_agent.scheduler import stop_scheduler

    stop_scheduler()

    try:
        await write_heartbeat(shutdown_type="graceful")
    except Exception as exc:
        logger.warning("shutdown_heartbeat_failed", error=str(exc))
    await AgentCore.close()
    await close_bot()
    await close_db()


app = FastAPI(
    title="Chronos Agent",
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
    routes=[Mount("/metrics", app=metrics_app)],
)


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request) -> Response:
    """
    Принимает обновления от Telegram.

    Безопасность: проверяет X-Telegram-Bot-Api-Secret-Token.
    Запросы без корректного токена -> 403.
    """
    if settings.telegram_webhook_secret_check:
        incoming_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        expected_secret = get_webhook_secret()
        if incoming_secret != expected_secret:
            logger.warning("webhook_auth_failed", reason="invalid_secret_token")
            return Response(status_code=403)

    body = await request.json()
    update = Update(**body)

    bot = get_bot()
    dp = get_dp()
    asyncio.create_task(dp.feed_update(bot=bot, update=update))

    return Response(status_code=200)


@app.get("/auth/google/callback")
async def google_oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    """
    Google OAuth 2.0 redirect callback.
    """
    if error:
        logger.warning("google_oauth_error", error=error)
        return HTMLResponse(OAUTH_ERROR_HTML.format(reason=error), status_code=400)

    if not code or not state:
        return HTMLResponse(
            OAUTH_ERROR_HTML.format(reason="missing code or state"),
            status_code=400,
        )

    try:
        user_id = verify_oauth_state(state)
    except ValueError as exc:
        logger.warning("google_oauth_state_invalid", error=str(exc))
        return HTMLResponse(OAUTH_ERROR_HTML.format(reason=str(exc)), status_code=400)

    try:
        tokens = await asyncio.to_thread(exchange_code, code)
    except ValueError as exc:
        logger.warning("google_oauth_exchange_failed", user_id=user_id, error=str(exc))
        return HTMLResponse(OAUTH_ERROR_HTML.format(reason=str(exc)), status_code=400)
    except Exception as exc:
        logger.error("google_oauth_exchange_error", user_id=user_id, error=str(exc))
        return HTMLResponse(
            OAUTH_ERROR_HTML.format(reason="token exchange failed"),
            status_code=500,
        )

    encrypted_token = encrypt_refresh_token(tokens.refresh_token)

    async with get_session() as session:
        user = await session.scalar(select(User).where(User.user_id == user_id))
        next_status = (
            "active" if user is not None and user.status == "active" else "pending_timezone"
        )
        await session.execute(
            update(User)
            .where(User.user_id == user_id)
            .values(
                gcal_refresh_token=encrypted_token,
                status=next_status,
            )
        )
        await session.commit()

    logger.info("google_oauth_completed", user_id=user_id, status=next_status)

    if settings.google_webhook_base_url:
        try:
            channel_info = await asyncio.to_thread(
                _register_calendar_channel_sync, user_id, tokens.refresh_token
            )
            if channel_info is not None:
                async with get_session() as session:
                    await session.execute(
                        update(User)
                        .where(User.user_id == user_id)
                        .values(
                            gcal_events_channel_id=channel_info.channel_id,
                            gcal_events_resource_id=channel_info.resource_id,
                            gcal_events_channel_expiry=channel_info.expiry,
                        )
                    )
                    await session.commit()
        except Exception as exc:
            logger.warning("gcal_channel_registration_error", user_id=user_id, error=str(exc))

    try:
        from chronos_agent.bot.texts import T

        bot = get_bot()
        if next_status == "active":
            await bot.send_message(
                int(user_id),
                "Google Calendar переподключён. "
                "Теперь повтори запрос, который не удалось выполнить.",
            )
        else:
            await bot.send_message(int(user_id), T.oauth_done_set_timezone)
    except Exception as exc:
        logger.warning("telegram_notify_failed", user_id=user_id, error=str(exc))

    return HTMLResponse(OAUTH_SUCCESS_HTML)


def _register_calendar_channel_sync(user_id: str, refresh_token: str):
    """
    Вспомогательная синхронная функция для вызова через asyncio.to_thread.
    Строит Calendar service из незашифрованного токена (только что полученного).
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    from chronos_agent.google.auth import SCOPES

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    calendar_service = build("calendar", "v3", credentials=creds)

    return register_calendar_events_channel(
        user_id=user_id,
        calendar_service=calendar_service,
        webhook_base_url=settings.google_webhook_base_url,
    )


@app.post("/webhooks/google/calendar")
async def google_calendar_push(request: Request) -> Response:
    """
    Получает push-уведомления от Google Calendar.
    """
    channel_id = request.headers.get("X-Goog-Channel-ID", "")
    resource_state = request.headers.get("X-Goog-Resource-State", "")
    message_number = request.headers.get("X-Goog-Message-Number", "")

    logger.info(
        "webhook_received",
        channel_id=channel_id,
        resource_state=resource_state,
        message_number=message_number,
    )

    if resource_state == "sync":
        return Response(status_code=200)

    if resource_state != "exists":
        return Response(status_code=200)

    if not channel_id:
        logger.warning("webhook_missing_channel_id")
        return Response(status_code=200)

    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.gcal_events_channel_id == channel_id)
        )
        user = result.scalar_one_or_none()

    if user is None:
        logger.warning("webhook_unknown_channel", channel_id=channel_id)
        return Response(status_code=200)

    if user.gcal_refresh_token is None:
        logger.warning("webhook_user_no_token", user_id=user.user_id)
        return Response(status_code=200)

    from chronos_agent.google.sync import sync_calendar_events

    try:
        count = await sync_calendar_events(user.user_id, user.gcal_refresh_token)
        logger.info("webhook_sync_completed", user_id=user.user_id, events_synced=count)
    except Exception as exc:
        logger.error("webhook_sync_failed", user_id=user.user_id, error=str(exc))

    return Response(status_code=200)


@app.get("/health")
async def health() -> JSONResponse:
    result = await run_all_checks(
        base_url=settings.mistral_base_url,
        api_key=settings.mistral_api_key,
    )
    status_code = 200 if result["status"] == "ok" else 503
    return JSONResponse(content=result, status_code=status_code)
