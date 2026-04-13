"""
Retry с exponential backoff для синхронных вызовов Google API.

Используется внутри asyncio.to_thread — sleep выполняется в потоке,
не блокируя event loop.

Политика:
  - Retryable: HTTP 429 и 5xx
  - Non-retryable: все остальные коды (400, 401, 403, 404 и т.д.)
  - Задержки: base_delay * 2^attempt (по умолчанию: 1 s, 2 s, 4 s)
  - После исчерпания попыток — поднимает CalendarAPIError
"""

import time
from collections.abc import Callable
from typing import Any, TypeVar

from googleapiclient.errors import HttpError

from chronos_agent.logging import get_logger
from chronos_agent.tools.exceptions import CalendarAPIError, OAuthExpiredError

logger = get_logger(__name__)

T = TypeVar("T")


def call_with_retry[T](
    fn: Callable[..., T],
    *args: Any,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    operation: str = "google_api_call",
    user_id: str = "",
    **kwargs: Any,
) -> T:
    """
    Вызывает fn(*args, **kwargs) с retry при retryable-ошибках.

    Используется внутри to_thread — это синхронная функция.
    operation и user_id — для логирования.

    Raises:
        CalendarAPIError — после исчерпания попыток или при non-retryable ошибке
        OAuthExpiredError — при HTTP 401
    """
    last_exc: CalendarAPIError | None = None

    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except HttpError as exc:
            status = exc.status_code

            if status == 401:
                raise OAuthExpiredError(f"Google OAuth token expired (401) during {operation}") from exc

            api_exc = CalendarAPIError(status, str(exc))

            if not api_exc.is_retryable():
                raise api_exc from exc

            last_exc = api_exc
            if attempt < max_attempts - 1:
                delay = base_delay * (2**attempt)
                logger.warning(
                    "google_api_retry",
                    operation=operation,
                    user_id=user_id,
                    status=status,
                    attempt=attempt + 1,
                    delay=delay,
                )
                time.sleep(delay)

    raise last_exc  # type: ignore[misc]
