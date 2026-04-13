"""
In-memory rate limiter на основе скользящего окна

Хранит timestamps последних N сообщений в deque на каждого пользователя.

Команды бота (/start, /cancel, /help, /status, /timezone) не учитываются.
"""

import time
from collections import defaultdict, deque
from threading import Lock


class SlidingWindowRateLimiter:
    def __init__(self, max_calls: int, window_seconds: int = 60) -> None:
        self._max_calls = max_calls
        self._window = window_seconds
        self._windows: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def is_allowed(self, user_id: str) -> bool:
        """
        Проверяет, разрешён ли следующий запрос от user_id.
        Если разрешён — записывает timestamp и возвращает True.
        Если превышен лимит — возвращает False.
        """
        now = time.monotonic()
        cutoff = now - self._window

        with self._lock:
            dq = self._windows[user_id]

            while dq and dq[0] <= cutoff:
                dq.popleft()

            if len(dq) >= self._max_calls:
                return False

            dq.append(now)
            return True

    def remaining(self, user_id: str) -> int:
        """Возвращает оставшееся число допустимых запросов в текущем окне."""
        now = time.monotonic()
        cutoff = now - self._window

        with self._lock:
            dq = self._windows[user_id]
            while dq and dq[0] <= cutoff:
                dq.popleft()
            return max(0, self._max_calls - len(dq))

    def reset(self, user_id: str) -> None:
        """Сбрасывает счётчик для пользователя (например, при /cancel)."""
        with self._lock:
            self._windows.pop(user_id, None)
