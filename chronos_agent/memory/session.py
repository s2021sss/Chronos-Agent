"""
Conversation session manager — жизненный цикл диалогов.

Функции:
  get_active_conversation       — найти активный/ожидающий диалог пользователя
  get_or_create_conversation    — получить существующий или создать новый диалог
  close_conversation            — закрыть диалог с указанной причиной
  touch_conversation            — обновить last_message_at
  update_conversation_status    — сменить статус (напр. active -> awaiting_user)
  close_expired_conversations   — cron: закрыть диалоги с истёкшим таймаутом
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from chronos_agent.db.engine import get_session
from chronos_agent.db.models import Conversation
from chronos_agent.logging import get_logger
from chronos_agent.memory.classify import ClassifyResult

logger = get_logger(__name__)


async def get_active_conversation(user_id: str) -> Conversation | None:
    """Возвращает единственный active/awaiting_user conversation пользователя."""
    async with get_session() as session:
        result = await session.execute(
            select(Conversation)
            .where(
                Conversation.user_id == user_id,
                Conversation.status.in_(("active", "awaiting_user")),
            )
            .order_by(Conversation.last_message_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def get_or_create_conversation(
    user_id: str,
    raw_input: str,
    classify_result: ClassifyResult,
) -> tuple[Conversation, bool]:
    """
    Возвращает (conversation, is_new).

    Если classify_result.decision == "continue" и есть активный диалог:
      - Обновляет last_message_at
      - Возвращает (active_conv, False)

    Если classify_result.decision == "new":
      - Закрывает предыдущий активный диалог (если есть) с reason="user_started_new"
      - Создаёт новый диалог
      - Возвращает (new_conv, True)
    """
    async with get_session() as session:
        result = await session.execute(
            select(Conversation)
            .where(
                Conversation.user_id == user_id,
                Conversation.status.in_(("active", "awaiting_user")),
            )
            .order_by(Conversation.last_message_at.desc())
            .limit(1)
        )
        active_conv = result.scalar_one_or_none()

        if classify_result.decision == "continue" and active_conv is not None:
            await session.execute(
                update(Conversation)
                .where(Conversation.id == active_conv.id)
                .values(last_message_at=datetime.now(UTC))
            )
            await session.commit()
            await session.refresh(active_conv)
            logger.info(
                "conversation_continued",
                user_id=user_id,
                conversation_id=active_conv.id,
                reason=classify_result.reason,
                used_llm=classify_result.used_llm,
            )
            return (active_conv, False)

        # Создаём новый диалог
        if active_conv is not None:
            await session.execute(
                update(Conversation)
                .where(Conversation.id == active_conv.id)
                .values(
                    status="completed",
                    closed_at=datetime.now(UTC),
                    closed_reason="user_started_new",
                )
            )
            logger.info(
                "conversation_superseded",
                user_id=user_id,
                closed_id=active_conv.id,
            )

        new_conv = Conversation(
            user_id=user_id,
            status="active",
            last_message_at=datetime.now(UTC),
        )
        session.add(new_conv)
        await session.flush()

        new_conv.thread_id = f"user:{user_id}:conv:{new_conv.id}"
        new_conv.langfuse_session_id = f"conv:{new_conv.id}"

        await session.commit()
        await session.refresh(new_conv)

        logger.info(
            "conversation_created",
            user_id=user_id,
            conversation_id=new_conv.id,
            reason=classify_result.reason,
        )
        return (new_conv, True)


async def touch_conversation(conversation_id: int) -> None:
    """Обновляет last_message_at = now."""
    async with get_session() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(last_message_at=datetime.now(UTC))
        )
        await session.commit()


async def update_conversation_status(conversation_id: int, status: str) -> None:
    """Устанавливает статус без закрытия (например active -> awaiting_user и обратно)."""
    async with get_session() as session:
        await session.execute(
            update(Conversation).where(Conversation.id == conversation_id).values(status=status)
        )
        await session.commit()
    logger.info("conversation_status_updated", conversation_id=conversation_id, status=status)


async def close_expired_conversations(timeout_minutes: int = 30) -> int:
    """
    Закрывает диалоги со статусом "active" где last_message_at < now - timeout.

    Диалоги со статусом "awaiting_user" (HITL) НЕ трогаются — их обрабатывает
    recover_orphaned_sessions() при следующем перезапуске сервиса.

    Возвращает количество закрытых диалогов.
    """
    threshold = datetime.now(UTC) - timedelta(minutes=timeout_minutes)

    async with get_session() as session:
        result = await session.execute(
            select(Conversation.id).where(
                Conversation.status == "active",
                Conversation.last_message_at < threshold,
            )
        )
        ids = list(result.scalars().all())

        if not ids:
            return 0

        await session.execute(
            update(Conversation)
            .where(Conversation.id.in_(ids))
            .values(
                status="expired",
                closed_at=datetime.now(UTC),
                closed_reason="timeout",
            )
        )
        await session.commit()

    logger.info(
        "conversations_expired_by_cron",
        count=len(ids),
        timeout_minutes=timeout_minutes,
    )
    return len(ids)
