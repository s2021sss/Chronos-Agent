"""
notify_user / request_confirmation — отправка сообщений и HITL-подтверждений.

notify_user:
  Отправляет сообщение пользователю через Telegram.
  Retry x2 при ошибке отправки (non-fatal — логирует, не падает).
  Возвращает message_id отправленного сообщения.

request_confirmation:
  Отправляет сообщение с кнопками ✅ Подтвердить / ❌ Отменить.
  callback_data формат: "confirm:<thread_id>" / "reject:<thread_id>"
  LangGraph ожидает ответа пользователя через checkpoint.
"""

import asyncio

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pydantic import BaseModel

from chronos_agent.bot.setup import get_bot
from chronos_agent.logging import get_logger

logger = get_logger(__name__)

_NOTIFY_RETRY_ATTEMPTS = 2
_NOTIFY_RETRY_DELAY = 1.0


class InlineButton(BaseModel):
    text: str
    callback_data: str


class NotifyInput(BaseModel):
    user_id: str
    text: str
    buttons: list[InlineButton] | None = None


async def notify_user(inp: NotifyInput) -> int | None:
    """
    Отправляет сообщение пользователю через Telegram Bot API.

    buttons: список InlineKeyboardButton (опционально)
    Возвращает message_id при успехе, None при неудаче (после retry).
    """
    bot = get_bot()

    keyboard: InlineKeyboardMarkup | None = None
    if inp.buttons:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=btn.text, callback_data=btn.callback_data)]
                for btn in inp.buttons
            ]
        )

    last_exc: Exception | None = None
    for attempt in range(_NOTIFY_RETRY_ATTEMPTS):
        try:
            msg = await bot.send_message(
                chat_id=int(inp.user_id),
                text=inp.text,
                reply_markup=keyboard,
            )
            logger.info("notify_user_sent", user_id=inp.user_id, message_id=msg.message_id)
            return msg.message_id
        except Exception as exc:
            last_exc = exc
            if attempt < _NOTIFY_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(_NOTIFY_RETRY_DELAY)

    logger.warning("notify_user_failed", user_id=inp.user_id, error=str(last_exc))
    return None


async def request_confirmation(
    user_id: str,
    text: str,
    thread_id: str,
) -> int | None:
    """
    Отправляет сообщение с кнопками HITL-подтверждения.

    thread_id:      идентификатор LangGraph thread (для восстановления checkpoint)

    callback_data:
      "confirm:<thread_id>" — пользователь подтвердил
      "reject:<thread_id>"  — пользователь отклонил

    Возвращает message_id кнопочного сообщения (для последующего удаления клавиатуры).
    """
    buttons = [
        InlineButton(text="✅ Подтвердить", callback_data=f"confirm:{thread_id}"),
        InlineButton(text="❌ Отменить", callback_data=f"reject:{thread_id}"),
    ]

    logger.info(
        "request_confirmation_sent",
        user_id=user_id,
        thread_id=thread_id,
    )

    return await notify_user(
        NotifyInput(
            user_id=user_id,
            text=text,
            buttons=buttons,
        )
    )
