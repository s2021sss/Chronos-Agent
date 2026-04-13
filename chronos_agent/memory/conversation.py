from sqlalchemy import delete, select

from chronos_agent.db.engine import get_session
from chronos_agent.db.models import ConversationMessage
from chronos_agent.logging import get_logger

logger = get_logger(__name__)

_MAX_STORED_MESSAGES_PER_USER = 50
_DEFAULT_HISTORY_LIMIT = 5


async def get_conversation_history(
    user_id: str,
    limit: int = _DEFAULT_HISTORY_LIMIT,
    conversation_id: int | None = None,
) -> list[dict]:
    """
    Возвращает последние сообщения в хронологическом порядке.

    Если conversation_id передан — возвращает только сообщения этого диалога.
    Если None — возвращает последние N без фильтра по диалогу.
    """
    async with get_session() as session:
        q = select(ConversationMessage).where(ConversationMessage.user_id == user_id)
        if conversation_id is not None:
            q = q.where(ConversationMessage.conversation_id == conversation_id)
        q = q.order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc()).limit(
            limit
        )
        result = await session.execute(q)
        messages = list(result.scalars().all())

    messages.reverse()
    return [
        {
            "role": message.role,
            "content": message.content,
        }
        for message in messages
    ]


async def add_conversation_message(
    user_id: str,
    role: str,
    content: str,
    conversation_id: int | None = None,
) -> None:
    """
    Сохраняет сообщение и удаляет старые записи сверх лимита.
    """
    content = content.strip()
    if not content:
        return

    if role not in ("user", "assistant"):
        logger.warning("conversation_message_invalid_role", user_id=user_id, role=role)
        return

    async with get_session() as session:
        session.add(
            ConversationMessage(
                user_id=user_id,
                role=role,
                content=content,
                conversation_id=conversation_id,
            )
        )
        await session.flush()

        old_ids = await session.execute(
            select(ConversationMessage.id)
            .where(ConversationMessage.user_id == user_id)
            .order_by(ConversationMessage.created_at.desc(), ConversationMessage.id.desc())
            .offset(_MAX_STORED_MESSAGES_PER_USER)
        )
        ids_to_delete = list(old_ids.scalars().all())
        if ids_to_delete:
            await session.execute(
                delete(ConversationMessage).where(ConversationMessage.id.in_(ids_to_delete))
            )

        await session.commit()
