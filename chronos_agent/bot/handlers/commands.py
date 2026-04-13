import asyncio
import zoneinfo

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import select

from chronos_agent.bot.texts import T
from chronos_agent.config import settings
from chronos_agent.db.engine import get_session
from chronos_agent.db.models import CalendarTask, User
from chronos_agent.google.auth import build_oauth_url, generate_oauth_state
from chronos_agent.logging import get_logger

logger = get_logger(__name__)

commands_router = Router(name="commands")


def _user_id(message: Message) -> str:
    """Telegram user ID как строка."""
    return str(message.from_user.id)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------


@commands_router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    uid = _user_id(message)
    logger.info("onboarding_started", user_id=uid)

    async with get_session() as session:
        result = await session.execute(select(User).where(User.user_id == uid))
        user = result.scalar_one_or_none()

        if user is None:
            # Первый раз — создаём запись и начинаем onboarding
            user = User(user_id=uid, status="pending_oauth")
            session.add(user)
            await session.commit()
            logger.info("onboarding_started", user_id=uid)

        if user.status == "pending_oauth":
            state = generate_oauth_state(uid)
            oauth_url = build_oauth_url(state)
            await message.answer(T.welcome)
            await message.answer(T.oauth_prompt.format(url=oauth_url), parse_mode="HTML")
            logger.info("oauth_flow_initiated", user_id=uid)

        elif user.gcal_refresh_token is None:
            state = generate_oauth_state(uid)
            oauth_url = build_oauth_url(state)
            await message.answer(T.oauth_reconnect_prompt.format(url=oauth_url), parse_mode="HTML")
            logger.info("oauth_reconnect_initiated", user_id=uid)

        elif user.status == "pending_timezone":
            await message.answer(T.timezone_pending)

        else:
            await message.answer(f"Рад снова тебя видеть!\n\n{T.help_text}")


@commands_router.message(Command("reconnect"))
async def cmd_reconnect(message: Message) -> None:
    uid = _user_id(message)
    logger.info("oauth_reconnect_requested", user_id=uid)

    async with get_session() as session:
        result = await session.execute(select(User).where(User.user_id == uid))
        user = result.scalar_one_or_none()

        if user is None:
            user = User(user_id=uid, status="pending_oauth")
            session.add(user)
            await session.commit()

    state = generate_oauth_state(uid)
    oauth_url = build_oauth_url(state)
    await message.answer(T.oauth_reconnect_prompt.format(url=oauth_url), parse_mode="HTML")
    logger.info("oauth_reconnect_initiated", user_id=uid)


# ---------------------------------------------------------------------------
# /cancel
# ---------------------------------------------------------------------------


@commands_router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    uid = _user_id(message)

    async with get_session() as session:
        result = await session.execute(select(User).where(User.user_id == uid))
        user = result.scalar_one_or_none()

        if user is None:
            await message.answer(T.cancel_no_session)
            return

    logger.info("session_cancelled", user_id=uid)
    await message.answer(T.cancel_done)


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------


@commands_router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(T.help_text)


# ---------------------------------------------------------------------------
# /status
# ---------------------------------------------------------------------------


@commands_router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    uid = _user_id(message)

    async with get_session() as session:
        user_result = await session.execute(select(User).where(User.user_id == uid))
        user = user_result.scalar_one_or_none()

        if user is None or user.status != "active":
            hint = (
                T.oauth_pending
                if (user is None or user.status == "pending_oauth")
                else T.timezone_pending
            )
            await message.answer(T.onboarding_blocked.format(hint=hint))
            return

        tasks_result = await session.execute(
            select(CalendarTask)
            .where(
                CalendarTask.user_id == uid,
                CalendarTask.status == "needsAction",
            )
            .order_by(CalendarTask.priority.desc(), CalendarTask.due_at.asc())
            .limit(10)
        )
        tasks = tasks_result.scalars().all()

    if not tasks:
        await message.answer(T.status_no_tasks)
        return

    lines = [T.status_header]
    for task in tasks:
        due_str = ""
        if task.due_at:
            due_str = f" — до {task.due_at.strftime('%d.%m %H:%M')}"

        priority_labels = {0: "", 1: " 🔵", 2: " 🟡", 3: " 🔴"}
        priority_str = priority_labels.get(task.priority, "")

        lines.append(
            T.status_task_row.format(
                title=task.title,
                due=due_str,
                priority=priority_str,
            )
        )

    await message.answer("".join(lines))


# ---------------------------------------------------------------------------
# /timezone
# ---------------------------------------------------------------------------


@commands_router.message(Command("timezone"))
async def cmd_timezone(message: Message, command: CommandObject) -> None:
    uid = _user_id(message)

    tz_arg = (command.args or "").strip()

    if not tz_arg:
        await message.answer(T.timezone_usage)
        return

    try:
        zoneinfo.ZoneInfo(tz_arg)
    except (zoneinfo.ZoneInfoNotFoundError, KeyError):
        await message.answer(T.timezone_invalid.format(tz=tz_arg))
        return

    async with get_session() as session:
        result = await session.execute(select(User).where(User.user_id == uid))
        user = result.scalar_one_or_none()

        if user is None:
            user = User(user_id=uid, status="pending_oauth", timezone=tz_arg)
            session.add(user)
        else:
            user.timezone = tz_arg
            if user.status == "pending_timezone":
                user.status = "active"
                await session.commit()

                logger.info("timezone_set", user_id=uid, timezone=tz_arg)
                logger.info("onboarding_completed", user_id=uid)

                await message.answer(T.timezone_set.format(tz=tz_arg))
                await message.answer(T.onboarding_complete)

                if (
                    settings.google_webhook_base_url
                    and user.gcal_refresh_token
                    and not user.gcal_events_channel_id
                ):
                    asyncio.create_task(
                        _register_gcal_channel_for_user(uid, user.gcal_refresh_token)
                    )

                return

        await session.commit()

    logger.info("timezone_set", user_id=uid, timezone=tz_arg)
    await message.answer(T.timezone_set.format(tz=tz_arg))


async def _register_gcal_channel_for_user(user_id: str, encrypted_token: bytes) -> None:
    """
    Фоновая задача: регистрирует Google Calendar push-канал после завершения онбординга.
    Вызывается через asyncio.create_task — не блокирует ответ пользователю.
    """
    from sqlalchemy import update as sa_update

    from chronos_agent.google.client import build_calendar_service
    from chronos_agent.google.webhooks import register_calendar_events_channel

    try:
        calendar_service = await asyncio.to_thread(build_calendar_service, encrypted_token)
        channel_info = await asyncio.to_thread(
            register_calendar_events_channel,
            user_id,
            calendar_service,
            settings.google_webhook_base_url,
        )
        if channel_info is not None:
            async with get_session() as session:
                await session.execute(
                    sa_update(User)
                    .where(User.user_id == user_id)
                    .values(
                        gcal_events_channel_id=channel_info.channel_id,
                        gcal_events_resource_id=channel_info.resource_id,
                        gcal_events_channel_expiry=channel_info.expiry,
                    )
                )
                await session.commit()
    except Exception as exc:
        logger.warning("gcal_channel_bg_registration_failed", user_id=user_id, error=str(exc))
