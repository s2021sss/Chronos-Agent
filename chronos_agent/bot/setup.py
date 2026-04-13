import hashlib

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from chronos_agent.bot.rate_limiter import SlidingWindowRateLimiter
from chronos_agent.config import settings
from chronos_agent.logging import get_logger

logger = get_logger(__name__)


_bot: Bot | None = None
_dp: Dispatcher | None = None
_rate_limiter: SlidingWindowRateLimiter | None = None


def get_bot() -> Bot:
    if _bot is None:
        raise RuntimeError("Bot not initialized. Call init_bot() first.")
    return _bot


def get_dp() -> Dispatcher:
    if _dp is None:
        raise RuntimeError("Dispatcher not initialized. Call init_bot() first.")
    return _dp


def get_rate_limiter() -> SlidingWindowRateLimiter:
    if _rate_limiter is None:
        raise RuntimeError("Rate limiter not initialized. Call init_bot() first.")
    return _rate_limiter


def get_webhook_secret() -> str:
    """
    Детерминированный секрет для X-Telegram-Bot-Api-Secret-Token.
    Производится из bot token через SHA-256 — не требует отдельной env-переменной.
    Telegram ограничивает длину токена 256 символами; первые 64 hex-символа достаточно.
    """
    return hashlib.sha256(settings.telegram_bot_token.encode()).hexdigest()[:64]


def init_bot() -> tuple[Bot, Dispatcher]:
    """
    Создаёт Bot и Dispatcher, регистрирует роутеры.
    Вызывать один раз в FastAPI lifespan startup.
    """
    global _bot, _dp, _rate_limiter

    from chronos_agent.bot.handlers.callbacks import callbacks_router
    from chronos_agent.bot.handlers.commands import commands_router
    from chronos_agent.bot.handlers.messages import messages_router

    _rate_limiter = SlidingWindowRateLimiter(
        max_calls=settings.rate_limit_msg_per_minute,
        window_seconds=60,
    )

    _bot = Bot(
        token=settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    _dp = Dispatcher()

    _dp.include_router(commands_router)
    _dp.include_router(callbacks_router)
    _dp.include_router(messages_router)

    logger.info("bot_initialized")
    return _bot, _dp


async def register_bot_commands() -> None:
    """Регистрирует команды для Telegram Bot Menu."""
    bot = get_bot()

    commands = [
        BotCommand(command="start", description="Начать настройку или показать приветствие"),
        BotCommand(command="help", description="Показать справку и примеры запросов"),
        BotCommand(command="status", description="Показать активные задачи"),
        BotCommand(command="reconnect", description="Переподключить Google Calendar"),
        BotCommand(command="timezone", description="Установить часовой пояс"),
        BotCommand(command="cancel", description="Отменить текущее действие"),
    ]

    await bot.set_my_commands(commands)
    logger.info("bot_commands_registered", count=len(commands))


async def register_webhook(webhook_url: str) -> None:
    """
    Регистрирует webhook в Telegram.
    webhook_url должен быть публичным HTTPS-адресом (ngrok в dev, домен в prod).

    В dev-режиме: передать TELEGRAM_WEBHOOK_URL через .env
    Пример: TELEGRAM_WEBHOOK_URL=https://xxxx.ngrok.io/webhook/telegram
    """
    bot = get_bot()

    secret = get_webhook_secret() if settings.telegram_webhook_secret_check else None

    await bot.set_webhook(
        url=webhook_url,
        secret_token=secret,
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=True,
    )
    logger.info(
        "webhook_registered",
        url=webhook_url,
        secret_check=settings.telegram_webhook_secret_check,
    )


async def close_bot() -> None:
    """Закрывает сессию Bot. Вызывать в FastAPI lifespan shutdown."""
    global _bot
    if _bot is not None:
        await _bot.session.close()
        _bot = None
        logger.info("bot_closed")
