from aiogram import F, Router
from aiogram.types import CallbackQuery

from chronos_agent.bot.texts import T
from chronos_agent.logging import get_logger

logger = get_logger(__name__)

callbacks_router = Router(name="callbacks")

_CONFIRM_PREFIX = "confirm:"
_REJECT_PREFIX = "reject:"


async def _safe_delete_message(message, *, user_id: str, reason: str) -> None:
    """Удаляет промежуточное сообщение, не ломая основной callback-flow."""
    if message is None:
        return
    try:
        await message.delete()
    except Exception as exc:
        logger.warning(
            "callback_cleanup_delete_failed",
            user_id=user_id,
            reason=reason,
            error=str(exc),
        )


def _validate_thread_for_user(thread_id: str, user_id: str) -> bool:
    """
    Проверяет что thread_id принадлежит данному пользователю.

    Форматы:
      "user:{uid}:conv:{cid}" — основной
      "user:{uid}"            — legacy
    """
    return thread_id.startswith(f"user:{user_id}:")


@callbacks_router.callback_query(F.data.startswith(_CONFIRM_PREFIX))
async def handle_confirm(callback: CallbackQuery) -> None:
    """Пользователь нажал подтверждение действия агента."""
    user_id = str(callback.from_user.id)
    thread_id = callback.data[len(_CONFIRM_PREFIX) :]  # type: ignore[index]

    if not _validate_thread_for_user(thread_id, user_id):
        logger.warning("callback_thread_mismatch", user_id=user_id, thread_id=thread_id)
        await callback.answer("Недействительный запрос.")
        return

    logger.info("confirmation_received", user_id=user_id, confirmed=True, thread_id=thread_id)

    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]

    progress_msg = await callback.message.answer(T.confirmation_confirmed)  # type: ignore[union-attr]
    await callback.answer()

    try:
        from chronos_agent.agent.core import AgentCore

        await AgentCore.resume(thread_id=thread_id, confirmed=True)
        await _safe_delete_message(callback.message, user_id=user_id, reason="confirm_prompt")  # type: ignore[arg-type]
        await _safe_delete_message(progress_msg, user_id=user_id, reason="confirm_progress")
    except Exception as exc:
        logger.error(
            "agent_resume_error",
            user_id=user_id,
            thread_id=thread_id,
            confirmed=True,
            error=str(exc),
        )
        await _safe_delete_message(callback.message, user_id=user_id, reason="confirm_prompt_error")  # type: ignore[arg-type]
        await _safe_delete_message(progress_msg, user_id=user_id, reason="confirm_progress_error")
        await callback.message.answer(T.generic_error)  # type: ignore[union-attr]


@callbacks_router.callback_query(F.data.startswith(_REJECT_PREFIX))
async def handle_reject(callback: CallbackQuery) -> None:
    """Пользователь нажал отмена действия агента."""
    user_id = str(callback.from_user.id)
    thread_id = callback.data[len(_REJECT_PREFIX) :]  # type: ignore[index]

    if not _validate_thread_for_user(thread_id, user_id):
        logger.warning("callback_thread_mismatch", user_id=user_id, thread_id=thread_id)
        await callback.answer("⚠️ Недействительный запрос.")
        return

    logger.info("confirmation_received", user_id=user_id, confirmed=False, thread_id=thread_id)

    await callback.message.edit_reply_markup(reply_markup=None)  # type: ignore[union-attr]
    await callback.message.answer(T.confirmation_rejected)  # type: ignore[union-attr]
    await callback.answer()

    try:
        from chronos_agent.agent.core import AgentCore

        await AgentCore.resume(thread_id=thread_id, confirmed=False)
    except Exception as exc:
        logger.warning(
            "agent_resume_reject_error",
            user_id=user_id,
            thread_id=thread_id,
            error=str(exc),
        )
    await _safe_delete_message(callback.message, user_id=user_id, reason="reject_prompt")  # type: ignore[arg-type]


@callbacks_router.callback_query()
async def handle_unknown_callback(callback: CallbackQuery) -> None:
    """Fallback для неизвестных callback_data — отвечаем, чтобы убрать часики."""
    logger.warning(
        "unknown_callback_data",
        user_id=str(callback.from_user.id),
        data=callback.data,
    )
    await callback.answer("Неизвестная команда.")
