"""
entry_router — главная точка входа для текстовых и голосовых сообщений.

Порядок обработки каждого входящего сообщения:
  1. Rate limit — проверяем скользящее окно; при превышении -> отказ
  2. Onboarding gate — если пользователь не active -> направляем к настройке
  3. HITL guard — если диалог ждёт подтверждения -> напоминаем нажать кнопку
  4. Trigger classification — text vs voice
  5. Для voice: транскрипция через Whisper
  6. Conversation classification — новый диалог или продолжение?
  7. Dispatch -> AgentCore
"""

import re
import time
from asyncio import Task
from datetime import UTC, datetime, timedelta

from aiogram import F, Router
from aiogram.types import Message
from sqlalchemy import select

from chronos_agent.bot.setup import get_rate_limiter
from chronos_agent.bot.texts import T
from chronos_agent.config import settings
from chronos_agent.db.engine import get_session
from chronos_agent.db.models import User
from chronos_agent.logging import get_logger
from chronos_agent.metrics import agent_request_duration_seconds, agent_requests_total
from chronos_agent.whisper import transcribe_voice

logger = get_logger(__name__)

messages_router = Router(name="messages")

# ---------------------------------------------------------------------------
# Off-topic pre-filter
# ---------------------------------------------------------------------------

_OFF_TOPIC_PATTERNS: list[re.Pattern[str]] = [
    # Вопросы о возможностях / личности бота
    re.compile(
        r"\b(что ты умеешь|чем (ты |вы )?(можешь|можете) помочь|что (ты |вы )?(можешь|умеешь)"
        r"|расскажи о себе|кто ты|ты кто|что такое chronos)\b",
        re.I,
    ),
    # Prompt injection / системный промпт
    re.compile(
        r"(покажи (системный |свой )?(промпт|prompt|инструкци)"
        r"|ignore (previous|prior|all)\s*(instructions?|rules?|prompts?)"
        r"|forget (your |all )?(previous |prior )?(instructions?|rules?)"
        r"|ты теперь|притворись|сыграй роль|действуй как|you are now|act as\b|dan\b)",
        re.I,
    ),
    # Запросы написать код / алгоритмы
    re.compile(
        r"\b(напиши (код|функци|скрипт|программ|алгоритм|сортировк|класс|метод)"
        r"|write (a |some )?(code|function|script|program|algorithm|class)"
        r"|реализуй|реализация|implement\b)",
        re.I,
    ),
    # Запросы написать текст не по теме
    re.compile(
        r"\b(напиши (сти(х|хи)|эссе|рассказ|стих|поэм|стихотворени|письмо|текст о)"
        r"|придумай (стих|историю|сказку|рассказ)"
        r"|переведи (текст|это|следующ)"
        r"|объясни (что такое|как работает|мне)\s+(?!создать|добавить|сделать)"
        r"|расскажи про\s+(?!chronos|календар|задач))\b",
        re.I,
    ),
]


def _is_off_topic(text: str) -> bool:
    """Возвращает True если сообщение явно не про планирование."""
    for pattern in _OFF_TOPIC_PATTERNS:
        if pattern.search(text):
            return True
    return False


async def _notify_background_agent_error(task: Task, message: Message, user_id: str) -> None:
    """Логирует ошибку фоновой обработки и сообщает пользователю."""
    try:
        task.result()
    except Exception as exc:
        logger.error("agent_iteration_background_error", user_id=user_id, error=str(exc))
        await message.answer(T.generic_error)


def _user_id(message: Message) -> str:
    return str(message.from_user.id)  # type: ignore[union-attr]


def _is_voice(message: Message) -> bool:
    return message.voice is not None


def _check_input_size(message: Message) -> str | None:
    """
    Проверяет ограничения входных данных.
    Возвращает описание ошибки или None если всё ок.
    """
    if message.text and len(message.text) > settings.max_text_length:
        return f"Текст слишком длинный (максимум {settings.max_text_length} символов)."

    if message.voice:
        size_bytes = message.voice.file_size or 0
        max_bytes = settings.max_audio_size_mb * 1024 * 1024
        if size_bytes > max_bytes:
            return f"Аудио слишком большое (максимум {settings.max_audio_size_mb} МБ)."

    return None


@messages_router.message(F.text | F.voice)
async def entry_router(message: Message) -> None:
    """
    Обрабатывает все текстовые и голосовые сообщения.
    Команды (/start, /cancel, ...) перехватываются раньше — в commands_router.
    """
    uid = _user_id(message)

    # ── Шаг 1: Rate limiting ──────────────────────────────────────────────────
    limiter = get_rate_limiter()
    if not limiter.is_allowed(uid):
        logger.warning("rate_limit_hit", user_id=uid)
        await message.answer(T.rate_limit)
        return

    # ── Шаг 2: Валидация размера ввода ────────────────────────────────────────
    size_error = _check_input_size(message)
    if size_error:
        logger.warning(
            "tool_validation_failed",
            user_id=uid,
            tool="entry_router",
            reason=size_error,
        )
        await message.answer(f"⚠️ {size_error}")
        return

    # ── Шаг 2.5: Off-topic pre-filter ────────────────────────────────────────
    if message.text and _is_off_topic(message.text):
        logger.info("off_topic_blocked", user_id=uid, text_preview=message.text[:80])
        await message.answer(T.off_topic)
        return

    # ── Шаг 3: Onboarding gate ────────────────────────────────────────────────
    async with get_session() as session:
        result = await session.execute(select(User).where(User.user_id == uid))
        user = result.scalar_one_or_none()

    if user is None or user.status == "pending_oauth":
        logger.info("onboarding_blocked_message", user_id=uid, status="pending_oauth")
        await message.answer(T.onboarding_blocked.format(hint=T.oauth_pending))
        return

    if user.status == "pending_timezone":
        logger.info("onboarding_blocked_message", user_id=uid, status="pending_timezone")
        await message.answer(T.onboarding_blocked.format(hint=T.timezone_pending))
        return

    # ── Шаг 3.5: HITL guard — пользователь прислал текст пока ждём кнопку ──
    from chronos_agent.memory.session import get_active_conversation

    _active_conv = await get_active_conversation(uid)
    if _active_conv is not None and _active_conv.status == "awaiting_user":
        logger.info("hitl_pending_text_ignored", user_id=uid, conversation_id=_active_conv.id)
        await message.answer(T.hitl_pending_reminder)
        return

    # ── Шаг 4: Classify trigger + транскрипция голосового ───────────────────
    raw_text: str | None = message.text

    if _is_voice(message):
        trigger = "voice_message"
        # Отправляем индикатор обработки
        thinking_msg = await message.answer(T.voice_transcribing)

        raw_text = await transcribe_voice(
            file_id=message.voice.file_id,  # type: ignore[union-attr]
            bot=message.bot,
        )

        # Удаляем сообщение-заглушку "распознаю..."
        try:
            await thinking_msg.delete()
        except Exception:
            pass

        if raw_text is None:
            logger.warning("voice_transcription_failed", user_id=uid)
            await message.answer(T.voice_transcription_failed)
            return
    else:
        trigger = "text_message"

    logger.info(
        "agent_iteration_started",
        user_id=uid,
        trigger=trigger,
        thread_id=f"user:{uid}",
        text_preview=(raw_text or "")[:80],
    )

    # ── Шаг 5: Conversation classification ────────────────────────────────────
    import asyncio

    from chronos_agent.memory.classify import classify_deterministic, classify_llm
    from chronos_agent.memory.conversation import get_conversation_history
    from chronos_agent.memory.session import get_or_create_conversation

    active_conv = await get_active_conversation(uid)
    silence = (
        datetime.now(UTC) - active_conv.last_message_at
        if active_conv is not None
        else timedelta(days=999)
    )

    classify_result = classify_deterministic(active_conv, raw_text or "", silence)

    if classify_result is None:
        # LLM fallback — только если детерминированный не дал ответа
        recent = await get_conversation_history(
            uid,
            limit=3,
            conversation_id=active_conv.id if active_conv else None,
        )
        classify_result = await classify_llm(active_conv, recent, raw_text or "", silence)

    conv, is_new = await get_or_create_conversation(uid, raw_text or "", classify_result)

    logger.info(
        "conversation_classified",
        user_id=uid,
        conversation_id=conv.id,
        is_new=is_new,
        decision=classify_result.decision,
        reason=classify_result.reason,
        used_llm=classify_result.used_llm,
    )

    # ── Шаг 6: Dispatch to AgentCore ─────────────────────────────────────────
    from chronos_agent.agent.core import AgentCore

    # Для голосовых сообщений — показываем транскрипт перед обработкой
    if trigger == "voice_message" and raw_text:
        await message.answer(T.voice_transcript.format(text=raw_text))

    agent_requests_total.labels(trigger=trigger).inc()
    _t0 = time.monotonic()
    agent_task = asyncio.create_task(
        AgentCore.run(
            user_id=uid,
            trigger=trigger,
            raw_input=raw_text or "",
            conversation_id=conv.id,
        )
    )
    try:
        await asyncio.wait_for(
            asyncio.shield(agent_task),
            timeout=settings.agent_iteration_timeout_seconds,
        )
    except TimeoutError:
        logger.warning("agent_iteration_timeout", user_id=uid)
        await message.answer(T.agent_still_processing)
        agent_task.add_done_callback(
            lambda task: asyncio.create_task(_notify_background_agent_error(task, message, uid))
        )
    except Exception as exc:
        logger.error("agent_iteration_error", user_id=uid, error=str(exc))
        await message.answer(T.generic_error)
    finally:
        agent_request_duration_seconds.labels(trigger=trigger).observe(time.monotonic() - _t0)
