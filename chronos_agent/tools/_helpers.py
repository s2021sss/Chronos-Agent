from sqlalchemy import select

from chronos_agent.db.engine import get_session
from chronos_agent.db.models import User
from chronos_agent.tools.exceptions import OAuthExpiredError


async def get_user_token(user_id: str) -> bytes:
    """
    Возвращает зашифрованный refresh_token пользователя из БД.
    Поднимает OAuthExpiredError если пользователь не авторизован.
    """
    async with get_session() as session:
        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()

    if user is None or user.gcal_refresh_token is None:
        raise OAuthExpiredError(f"No OAuth token for user {user_id}. Re-authorization required.")

    return user.gcal_refresh_token


async def get_user(user_id: str) -> User:
    """
    Возвращает запись User из БД.
    Поднимает OAuthExpiredError если пользователь не найден или не авторизован.
    """
    async with get_session() as session:
        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()

    if user is None or user.gcal_refresh_token is None:
        raise OAuthExpiredError(f"User {user_id} not found or not authorized.")

    return user
