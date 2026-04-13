import asyncio

import httpx
import sqlalchemy as sa

from chronos_agent.logging import get_logger

logger = get_logger(__name__)

_LLM_CHECK_TIMEOUT = 5.0


async def check_postgres() -> tuple[str, str | None]:
    """
    Проверяет доступность PostgreSQL через SELECT 1.
    """
    try:
        from chronos_agent.db.engine import get_session

        async with get_session() as session:
            await session.execute(sa.text("SELECT 1"))
        return "ok", None
    except Exception as exc:
        logger.warning("health_postgres_check_failed", error=str(exc))
        return "error", str(exc)


async def check_llm(base_url: str, api_key: str) -> tuple[str, str | None]:
    """
    Проверяет доступность LLM API через GET /models.

    Работает с OpenAI-compatible API через GET /models.

    Не проверяет корректность ключа — только TCP-доступность и HTTP-ответ.
    401 и 429 считаются "ok" (сервис отвечает).
    Таймаут и сетевые ошибки — "error".
    """
    url = base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        async with httpx.AsyncClient(timeout=_LLM_CHECK_TIMEOUT) as client:
            response = await client.get(url, headers=headers)
            if response.status_code < 500:
                return "ok", None
            return "error", f"HTTP {response.status_code}"
    except httpx.TimeoutException:
        logger.warning("health_llm_check_timeout", url=url)
        return "error", "timeout"
    except Exception as exc:
        logger.warning("health_llm_check_failed", error=str(exc))
        return "error", str(exc)


async def run_all_checks(base_url: str, api_key: str) -> dict:
    """
    Запускает все проверки параллельно.

    Возвращает:
    {
        "status": "ok" | "degraded",
        "postgres": "ok" | "<error>",
        "llm": "ok" | "<error>",
    }

    "degraded" — хотя бы одна зависимость недоступна.
    """
    pg_result, llm_result = await asyncio.gather(
        check_postgres(),
        check_llm(base_url, api_key),
    )

    pg_ok = pg_result[0] == "ok"
    llm_ok = llm_result[0] == "ok"

    overall = "ok" if (pg_ok and llm_ok) else "degraded"

    return {
        "status": overall,
        "postgres": "ok" if pg_ok else pg_result[1],
        "llm": "ok" if llm_ok else llm_result[1],
    }
